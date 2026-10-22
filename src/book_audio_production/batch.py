"""Deterministic local orchestration for validated audio batches."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from .artifacts import publish_artifact, write_manifest_atomic
from .engine import _estimate_output_bytes, render_track
from .errors import AudioError
from .manifest import parse_manifest
from .models import (
    AudioBatch,
    AudioBatchResult,
    AudioLimits,
    BatchStatus,
    ErrorInfo,
    MeasurementStatus,
    QAResult,
    TrackRequest,
    TrackResult,
    VerificationResult,
)
from .probe import verified_toolchain
from .report import serialize_result
from .runner import run_process
from .validation import validate_output_locations, validate_roots, validate_track

_RECIPE_VERSION = "audio-v1c"


def _canonical_batch(batch: AudioBatch) -> AudioBatch:
    if not isinstance(batch, AudioBatch):
        raise AudioError("INVALID_MANIFEST", "Audio batch request is invalid")
    try:
        payload = {"schema_version": "audio-batch.v1", **asdict(batch)}
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise AudioError("INVALID_MANIFEST", "Audio batch request is invalid") from None
    return parse_manifest(encoded.encode("utf-8"))


def _safe_reference(source: str) -> str | None:
    if not isinstance(source, str) or not source:
        return None
    normalized = source.replace("\\", "/")
    parsed = urlsplit(normalized)
    path = PurePosixPath(normalized)
    if parsed.scheme or parsed.netloc or path.is_absolute() or PureWindowsPath(source).is_absolute():
        return None
    if any(part == ".." for part in normalized.split("/")):
        return None
    return path.as_posix()


def fingerprint_sources(
    batch: AudioBatch,
    input_root: Path,
    limits: AudioLimits,
    *,
    deadline: float | None = None,
) -> dict[str, str | None]:
    """Hash safe, size-bounded local inputs without probing or launching media tools."""
    try:
        root = Path(input_root).resolve(strict=True)
    except OSError:
        return {track.track_id: None for track in batch.tracks}
    hashes: dict[str, str | None] = {}
    for track in batch.tracks:
        reference = _safe_reference(track.source)
        if reference is None:
            hashes[track.track_id] = None
            continue
        try:
            source = (root / reference).resolve(strict=True)
            source.relative_to(root)
            size = source.stat().st_size
            if not source.is_file() or not 0 < size <= limits.max_source_bytes:
                hashes[track.track_id] = None
                continue
            digest = hashlib.sha256()
            with source.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise AudioError("PROCESS_TIMEOUT", "Audio batch exceeded its time limit", retryable=True)
                    digest.update(chunk)
            hashes[track.track_id] = digest.hexdigest()
        except AudioError:
            raise
        except (OSError, RuntimeError, ValueError):
            hashes[track.track_id] = None
    return hashes


def _fingerprint(batch: AudioBatch, limits: AudioLimits, ffmpeg: str, ffprobe: str, hashes: dict[str, str | None]) -> str:
    request = asdict(batch)
    document = {
        "recipe_version": _RECIPE_VERSION,
        "request": request,
        "source_sha256": hashes,
        "limits": asdict(limits),
        "ffmpeg_version": ffmpeg,
        "ffprobe_version": ffprobe,
    }
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rejected_track(request: TrackRequest, error: AudioError, seconds: float) -> TrackResult:
    return TrackResult(
        track_id=request.track_id,
        status="rejected",
        source_relative=_safe_reference(request.source),
        source_sha256=None,
        trim=request.trim,
        requested_targets=request.targets,
        pre_measurement=None,
        post_measurement=None,
        output_format=request.output_format,
        output_relative_path=None,
        output_sha256=None,
        output_bytes=0,
        normalization_type=None,
        qa=QAResult(False, (error.code,), {error.code: False}),
        wall_seconds=max(0.0, seconds),
        cpu_seconds=None,
        measurement_status="unavailable",
        warnings=(),
        error=ErrorInfo(error.code, str(error), error.retryable),
    )


def _deadline_runner(deadline: float, calls: list):
    """Return a process runner bounded by ``deadline`` that records every result in ``calls``."""

    def tracked_runner(argv, timeout_seconds, output_limit_bytes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AudioError("PROCESS_TIMEOUT", "Audio track exceeded its time limit", retryable=True)
        result = run_process(argv, min(timeout_seconds, remaining), output_limit_bytes)
        calls.append(result)
        return result

    return tracked_runner


def _validate_result_cpu(results: list[Any]) -> tuple[float | None, MeasurementStatus]:
    if not results or any(result.measurement_status != "measured" or result.cpu_seconds is None for result in results):
        return None, "unavailable"
    return sum(result.cpu_seconds for result in results), "measured"


def process_batch_once(
    batch: AudioBatch,
    input_root: Path,
    output_root: Path,
    limits: AudioLimits,
    *,
    deadline: float | None = None,
    started_at: float | None = None,
) -> AudioBatchResult:
    """Stage one batch attempt and atomically expose it only after verification."""
    from .limits import validate_limits

    normalized = _canonical_batch(batch)
    validate_limits(limits)
    start = time.monotonic() if started_at is None else started_at
    batch_deadline = start + limits.batch_deadline_seconds if deadline is None else deadline
    source_root, output_root = validate_roots(Path(input_root), Path(output_root))
    batch_dir = output_root / normalized.batch_id
    validate_output_locations(output_root, normalized.batch_id)
    if batch_dir.exists():
        raise AudioError("OUTPUT_EXISTS", "Batch output already exists")
    staging_root = output_root / ".staging" / normalized.batch_id
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=staging_root))
    except OSError:
        raise AudioError("OUTPUT_UNAVAILABLE", "Batch staging directory cannot be created") from None
    try:
        return _process_batch_in_stage(
            normalized,
            source_root,
            output_root,
            limits,
            stage_dir,
            batch_dir,
            start,
            batch_deadline,
        )
    finally:
        if stage_dir.exists() and not stage_dir.is_symlink():
            shutil.rmtree(stage_dir, ignore_errors=True)
        for directory in (staging_root, staging_root.parent):
            with contextlib.suppress(OSError):
                directory.rmdir()


def discard_incomplete_staging(output_root: Path, batch_id: str) -> None:
    """Remove only this batch's reserved staging tree after acquiring its lock."""
    staging_root = Path(output_root) / ".staging" / batch_id
    if staging_root.is_symlink():
        raise AudioError("UNSAFE_PATH", "Batch staging path must not be a symbolic link")
    if staging_root.exists():
        try:
            shutil.rmtree(staging_root)
        except OSError:
            raise AudioError("OUTPUT_UNAVAILABLE", "Incomplete batch staging data cannot be cleared") from None


def _publish_batch_directory(stage_dir: Path, batch_dir: Path, *, deadline: float) -> None:
    """Atomically publish a verified batch, retrying short-lived filesystem errors."""
    for attempt in range(4):
        # POSIX rename() silently replaces an existing empty directory, so an
        # existing destination is refused up front; the batch lock is held.
        if os.path.lexists(batch_dir):
            raise AudioError("OUTPUT_EXISTS", "Batch output already exists")
        try:
            os.rename(stage_dir, batch_dir)
            return
        except FileExistsError:
            raise AudioError("OUTPUT_EXISTS", "Batch output already exists") from None
        except OSError as error:
            # POSIX reports a non-empty destination directory as ENOTEMPTY
            # (or EEXIST) instead of FileExistsError.
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise AudioError("OUTPUT_EXISTS", "Batch output already exists") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AudioError("PROCESS_TIMEOUT", "Audio batch exceeded its time limit", retryable=True) from None
            if attempt == 3:
                raise AudioError("OUTPUT_UNAVAILABLE", "Verified batch could not be published") from None
            time.sleep(min(0.05 * (2**attempt), remaining))


def _process_batch_in_stage(
    normalized: AudioBatch,
    source_root: Path,
    output_root: Path,
    limits: AudioLimits,
    stage_dir: Path,
    batch_dir: Path,
    start: float,
    batch_deadline: float,
) -> AudioBatchResult:
    from .limits import validate_batch_capacity

    remaining = batch_deadline - time.monotonic()
    if remaining <= 0:
        raise AudioError("PROCESS_TIMEOUT", "Audio batch exceeded its time limit", retryable=True)
    toolchain = verified_toolchain(timeout_seconds=remaining)
    manifest_path = stage_dir / "result.json"

    validated = {}
    errors: dict[str, AudioError] = {}
    process_results: dict[str, list] = {}
    source_hashes = fingerprint_sources(normalized, source_root, limits, deadline=batch_deadline)
    track_deadlines: dict[str, float] = {}
    total_duration = 0.0
    predicted_batch_bytes = 0
    for request in normalized.tracks:
        track_start = time.monotonic()
        track_deadline = min(batch_deadline, track_start + limits.track_deadline_seconds)
        track_deadlines[request.track_id] = track_deadline
        calls: list[Any] = []

        if time.monotonic() >= track_deadline:
            errors[request.track_id] = AudioError("PROCESS_TIMEOUT", "Audio batch exceeded its time limit", retryable=True)
            process_results[request.track_id] = calls
            process_results[f"wall:{request.track_id}"] = [max(0.0, time.monotonic() - track_start)]
            continue

        tracked_runner = _deadline_runner(track_deadline, calls)

        try:
            item = validate_track(
                request,
                source_root,
                limits,
                tracked_runner,
                toolchain=toolchain,
                deadline=track_deadline,
            )
            estimate = _estimate_output_bytes(item)
            if estimate > limits.max_output_bytes_per_track:
                raise AudioError("RESOURCE_LIMIT_EXCEEDED", "Rendered audio may exceed its size limit")
            validated[request.track_id] = item
            source_hashes[request.track_id] = item.source_sha256
            process_results[request.track_id] = calls
            total_duration += item.probe.duration_seconds
            predicted_batch_bytes += estimate
        except AudioError as error:
            errors[request.track_id] = error
            process_results[request.track_id] = calls
        except OSError:
            errors[request.track_id] = AudioError("SOURCE_NOT_FOUND", "Source audio file could not be inspected")
            process_results[request.track_id] = calls
        # Keep the preflight timing for rejected tracks even when no media process ran.
        process_results[f"wall:{request.track_id}"] = [max(0.0, time.monotonic() - track_start)]

    validate_batch_capacity(len(normalized.tracks), total_duration, limits)
    if predicted_batch_bytes > limits.max_output_bytes_per_batch:
        raise AudioError("RESOURCE_LIMIT_EXCEEDED", "Batch output may exceed its configured size limit")

    fingerprint = _fingerprint(normalized, limits, toolchain.ffmpeg_version, toolchain.ffprobe_version, source_hashes)
    artifact_dir = stage_dir / "artifacts"
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AudioError("OUTPUT_UNAVAILABLE", "Artifact directory cannot be created") from None

    track_results: list[TrackResult] = []
    for request in normalized.tracks:
        track_start = time.monotonic()
        if request.track_id in errors:
            duration = process_results.get(f"wall:{request.track_id}", [0.0])[0]
            track_results.append(_rejected_track(request, errors[request.track_id], duration))
            continue

        item = validated[request.track_id]
        calls = process_results[request.track_id]
        track_deadline = track_deadlines[request.track_id]
        if time.monotonic() >= track_deadline:
            timeout_error = AudioError("PROCESS_TIMEOUT", "Audio track exceeded its time limit", retryable=True)
            track_results.append(_rejected_track(request, timeout_error, time.monotonic() - track_start))
            continue

        tracked_runner = _deadline_runner(track_deadline, calls)

        extension = ".flac" if request.output_format == "flac" else ".wav"
        filename = f"{request.track_id}-{item.source_sha256[:12]}{extension}"
        final_output = artifact_dir / filename
        source_stage = None
        try:
            source_stage = Path(tempfile.mkdtemp(prefix=f".{request.track_id}-", dir=stage_dir))
            rendered = render_track(
                item,
                source_stage / filename,
                tracked_runner,
                toolchain=toolchain,
                deadline=track_deadline,
            )
            publish_artifact(source_stage / filename, final_output)
            relative_output = PurePosixPath("artifacts", filename).as_posix()
            cpu_seconds, measurement_status = _validate_result_cpu(calls)
            track_results.append(
                replace(
                    rendered,
                    output_relative_path=relative_output,
                    wall_seconds=(process_results.get(f"wall:{request.track_id}", [0.0])[0] + max(0.0, time.monotonic() - track_start)),
                    cpu_seconds=cpu_seconds,
                    measurement_status=measurement_status,
                )
            )
        except AudioError as error:
            track_results.append(_rejected_track(request, error, time.monotonic() - track_start))
        except OSError:
            publish_error = AudioError("OUTPUT_UNAVAILABLE", "Audio artifact could not be published")
            track_results.append(_rejected_track(request, publish_error, time.monotonic() - track_start))
        finally:
            if source_stage is not None:
                shutil.rmtree(source_stage, ignore_errors=True)

    accepted = sum(track.status == "accepted" for track in track_results)
    rejected = len(track_results) - accepted
    status: BatchStatus = "accepted" if rejected == 0 else "rejected" if accepted == 0 else "completed_with_errors"
    flat_processes = [result for values in process_results.values() for result in values if hasattr(result, "cpu_seconds")]
    cpu_seconds, measurement_status = _validate_result_cpu(flat_processes)
    result = AudioBatchResult(
        schema_version="audio-batch-result.v1",
        batch_id=normalized.batch_id,
        status=status,
        recipe_version=_RECIPE_VERSION,
        fingerprint=fingerprint,
        ffmpeg_version=toolchain.ffmpeg_version,
        ffprobe_version=toolchain.ffprobe_version,
        model_calls=0,
        accepted_tracks=accepted,
        rejected_tracks=rejected,
        wall_seconds=max(0.0, time.monotonic() - start),
        cpu_seconds=cpu_seconds,
        measurement_status=measurement_status,
        bytes_written=sum(track.output_bytes for track in track_results if track.status == "accepted"),
        monetary_cost=None,
        cost_status="unknown",
        replay=False,
        tracks=tuple(track_results),
    )
    write_manifest_atomic(manifest_path, serialize_result(result))
    verification = VerificationResult(True, None, 0, 0)
    if accepted:
        from .artifacts import verify_manifest

        verification = verify_manifest(manifest_path)
    if not verification.passed:
        raise AudioError(verification.error_code or "REPLAY_INTEGRITY_ERROR", "Published audio artifacts failed verification")
    if time.monotonic() >= batch_deadline:
        raise AudioError("PROCESS_TIMEOUT", "Audio batch exceeded its time limit", retryable=True)
    _publish_batch_directory(stage_dir, batch_dir, deadline=batch_deadline)
    return result
