"""Atomic publication and integrity verification for local result artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeGuard
from urllib.parse import urlsplit

from .errors import AudioError
from .models import VerificationResult


def publish_artifact(staged: Path, final: Path) -> None:
    """Publish a completed artifact atomically without replacing an existing file."""
    try:
        if not staged.is_file():
            raise AudioError("OUTPUT_PUBLISH_FAILED", "Staged audio artifact is unavailable")
        final.parent.mkdir(parents=True, exist_ok=True)
        os.link(staged, final)
        staged.unlink()
    except AudioError:
        raise
    except FileExistsError:
        raise AudioError("OUTPUT_EXISTS", "Output file already exists") from None
    except OSError:
        raise AudioError("OUTPUT_PUBLISH_FAILED", "Staged audio artifact could not be published") from None


def write_manifest_atomic(manifest: Path, payload: dict[str, object]) -> None:
    """Write and publish a JSON manifest atomically, never replacing prior work."""
    sidecar = manifest.with_name(f"{manifest.name}.sha256")
    sidecar_created = False
    temporary_paths: list[Path] = []
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        content = (encoded + "\n").encode("utf-8")
        sidecar_content = (hashlib.sha256(content).hexdigest() + "\n").encode("ascii")
        if manifest.exists() or sidecar.exists():
            raise FileExistsError
        manifest.parent.mkdir(parents=True, exist_ok=True)
        for suffix, data in ((".manifest.tmp", content), (".sha256.tmp", sidecar_content)):
            handle, temporary_name = tempfile.mkstemp(prefix=f".{manifest.name}.", suffix=suffix, dir=manifest.parent)
            temporary = Path(temporary_name)
            temporary_paths.append(temporary)
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        os.link(temporary_paths[1], sidecar)
        sidecar_created = True
        os.link(temporary_paths[0], manifest)
    except FileExistsError:
        if sidecar_created:
            sidecar.unlink(missing_ok=True)
        raise AudioError("OUTPUT_EXISTS", "Result manifest already exists") from None
    except (OSError, TypeError, ValueError):
        if sidecar_created:
            sidecar.unlink(missing_ok=True)
        raise AudioError("MANIFEST_WRITE_FAILED", "Result manifest could not be written") from None
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def _safe_artifact_reference(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    parsed = urlsplit(value)
    path = PurePosixPath(value)
    return (
        not parsed.scheme
        and not parsed.netloc
        and not path.is_absolute()
        and not PureWindowsPath(value).is_absolute()
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _digest(path: Path, *, deadline: float | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            if deadline is not None and time.monotonic() >= deadline:
                raise AudioError("PROCESS_TIMEOUT", "Audio batch exceeded its time limit", retryable=True)
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_manifest(manifest: Path, *, deadline: float | None = None) -> VerificationResult:
    """Check accepted artifact paths, SHA-256 digests, and byte counts."""
    if deadline is not None and time.monotonic() >= deadline:
        return VerificationResult(False, "PROCESS_TIMEOUT", 0, 0)
    if not manifest.is_file():
        return VerificationResult(False, "MANIFEST_NOT_FOUND", 0, 0)
    sidecar = manifest.with_name(f"{manifest.name}.sha256")
    if manifest.is_symlink() or sidecar.is_symlink():
        return VerificationResult(False, "UNSAFE_PATH", 0, 0)
    try:
        if manifest.stat().st_size > 4 * 1024 * 1024 or sidecar.stat().st_size > 128:
            return VerificationResult(False, "INVALID_MANIFEST", 0, 0)
        manifest_bytes = manifest.read_bytes()
        expected_digest = sidecar.read_text(encoding="ascii").strip()
        if not hmac.compare_digest(hashlib.sha256(manifest_bytes).hexdigest(), expected_digest):
            return VerificationResult(False, "REPLAY_INTEGRITY_ERROR", 0, 0)
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        code = "REPLAY_INTEGRITY_ERROR" if manifest.with_name(f"{manifest.name}.sha256").exists() is False else "INVALID_MANIFEST"
        return VerificationResult(False, code, 0, 0)
    if not isinstance(payload, dict) or payload.get("schema_version") != "audio-batch-result.v1":
        return VerificationResult(False, "INVALID_MANIFEST", 0, 0)
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return VerificationResult(False, "INVALID_MANIFEST", 0, 0)

    checked = 0
    total_bytes = 0
    root = manifest.parent.resolve()
    for track in tracks:
        if not isinstance(track, dict):
            return VerificationResult(False, "INVALID_MANIFEST", checked, total_bytes)
        if track.get("status") != "accepted":
            continue
        reference = track.get("output_relative_path")
        digest = track.get("output_sha256")
        expected_size = track.get("output_bytes")
        if not _safe_artifact_reference(reference):
            return VerificationResult(False, "UNSAFE_PATH", checked, total_bytes)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            return VerificationResult(False, "INVALID_MANIFEST", checked, total_bytes)
        artifact = manifest.parent.joinpath(*PurePosixPath(reference).parts)
        try:
            resolved = artifact.resolve(strict=True)
            resolved.relative_to(root)
            if artifact.is_symlink() or not resolved.is_file():
                return VerificationResult(False, "UNSAFE_PATH", checked, total_bytes)
            actual_digest, actual_size = _digest(resolved, deadline=deadline)
        except AudioError as error:
            return VerificationResult(False, error.code, checked, total_bytes)
        except (OSError, RuntimeError, ValueError):
            return VerificationResult(False, "REPLAY_INTEGRITY_ERROR", checked, total_bytes)
        if actual_digest != digest or actual_size != expected_size:
            return VerificationResult(False, "REPLAY_INTEGRITY_ERROR", checked + 1, total_bytes + actual_size)
        checked += 1
        total_bytes += actual_size
    if deadline is not None and time.monotonic() >= deadline:
        return VerificationResult(False, "PROCESS_TIMEOUT", checked, total_bytes)
    return VerificationResult(True, None, checked, total_bytes)
