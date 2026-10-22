"""Strict parser for the versioned audio batch contract."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, NoReturn, cast

from .errors import AudioError
from .models import AudioBatch, LoudnessTargets, OutputFormat, TrackRequest, TrimRange

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
_FORMATS = {"flac", "wav_pcm24"}
# The batch id names the output folder and lock file; Windows cannot create a
# folder or file whose stem is one of these device names.
_WINDOWS_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(10)), *(f"LPT{n}" for n in range(10))})
MAX_MANIFEST_BYTES = 1024 * 1024


def _invalid(message: str = "Audio batch manifest is invalid") -> NoReturn:
    raise AudioError("INVALID_MANIFEST", message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def _object(value: Any, allowed: set[str], required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(f"{label} must be an object")
    missing = required.difference(value)
    unknown = set(value).difference(allowed)
    if missing or unknown:
        _invalid(f"{label} has missing or unknown fields")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _invalid(f"{label} must contain 1 to 64 ASCII letters, digits, underscores, or hyphens")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        _invalid(f"{label} must be a finite number")
    if not math.isfinite(number):
        _invalid(f"{label} must be a finite number")
    return number


def _milliseconds(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"{label} must be an integer or null")
    return value


def parse_manifest(payload: bytes) -> AudioBatch:
    """Parse a UTF-8 ``audio-batch.v1`` manifest into immutable models."""
    if not isinstance(payload, bytes):
        _invalid()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise AudioError("RESOURCE_LIMIT_EXCEEDED", "Audio batch manifest exceeds its size limit")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _invalid()

    root = _object(
        document,
        {"schema_version", "batch_id", "tracks"},
        {"schema_version", "batch_id", "tracks"},
        "manifest",
    )
    if root["schema_version"] != "audio-batch.v1":
        raise AudioError("UNSUPPORTED_SCHEMA", "Unsupported audio batch schema version")
    batch_id = _identifier(root["batch_id"], "batch_id")
    if batch_id.upper() in _WINDOWS_RESERVED_NAMES:
        _invalid("batch_id must not be a reserved Windows device name")
    raw_tracks = root["tracks"]
    if not isinstance(raw_tracks, list) or not 1 <= len(raw_tracks) <= 20:
        _invalid("tracks must contain between 1 and 20 items")

    tracks: list[TrackRequest] = []
    track_ids: set[str] = set()
    for raw_track in raw_tracks:
        track = _object(
            raw_track,
            {"track_id", "source", "trim", "targets", "output_format"},
            {"track_id", "source", "targets", "output_format"},
            "track",
        )
        track_id = _identifier(track["track_id"], "track_id")
        # Output files are named "<track_id>-<source hash>", so "Intro" and
        # "intro" mastered from one source would overwrite each other on
        # case-insensitive file systems (macOS, Windows).
        if track_id.casefold() in track_ids:
            raise AudioError("DUPLICATE_TRACK_ID", "Track identifiers must be unique within a batch, ignoring case")
        track_ids.add(track_id.casefold())

        source = track["source"]
        if not isinstance(source, str) or not source.strip():
            _invalid("source must be a non-empty relative path")

        raw_trim = track.get("trim")
        trim: TrimRange | None = None
        if raw_trim is not None:
            trim_obj = _object(raw_trim, {"start_ms", "end_ms"}, set(), "trim")
            trim = TrimRange(
                _milliseconds(trim_obj.get("start_ms"), "trim.start_ms"),
                _milliseconds(trim_obj.get("end_ms"), "trim.end_ms"),
            )

        raw_targets = _object(
            track["targets"],
            {"integrated_lufs", "true_peak_dbtp", "lra_lu"},
            {"integrated_lufs", "true_peak_dbtp"},
            "targets",
        )
        lra_value = raw_targets.get("lra_lu")
        targets = LoudnessTargets(
            integrated_lufs=_number(raw_targets["integrated_lufs"], "targets.integrated_lufs"),
            true_peak_dbtp=_number(raw_targets["true_peak_dbtp"], "targets.true_peak_dbtp"),
            lra_lu=None if lra_value is None else _number(lra_value, "targets.lra_lu"),
        )

        output_format = track["output_format"]
        if not isinstance(output_format, str) or output_format not in _FORMATS:
            raise AudioError("INVALID_OUTPUT_FORMAT", "Output format must be flac or wav_pcm24")
        tracks.append(TrackRequest(track_id, source, trim, targets, cast(OutputFormat, output_format)))

    return AudioBatch(batch_id=batch_id, tracks=tuple(tracks))


def read_manifest_file(path: Path) -> AudioBatch:
    """Read at most one MiB plus one byte before parsing a local manifest."""
    try:
        with Path(path).open("rb") as file:
            payload = file.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise AudioError("INVALID_MANIFEST", "Audio batch manifest cannot be read") from None
    return parse_manifest(payload)
