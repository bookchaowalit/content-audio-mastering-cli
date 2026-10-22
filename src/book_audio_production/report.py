"""Safe conversion of internal results to the public JSON contract."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from .errors import AudioError
from .models import (
    AudioBatchResult,
    AudioMeasurement,
    ErrorInfo,
    LoudnessTargets,
    QAResult,
    TrackResult,
    TrimRange,
)


def _safe_relative_reference(value: object) -> bool:
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


def serialize_result(result: AudioBatchResult) -> dict[str, object]:
    """Return JSON-ready data without commands, host paths, or non-finite numbers."""
    if result.model_calls != 0 or result.monetary_cost is not None or result.cost_status != "unknown":
        raise AudioError("UNSAFE_REPORT", "Audio report cost and model-call fields are inconsistent")
    try:
        payload = asdict(result)
        for track in payload["tracks"]:
            for field in ("source_relative", "output_relative_path"):
                reference = track[field]
                if reference is not None and not _safe_relative_reference(reference):
                    raise AudioError("UNSAFE_REPORT", "Audio report contains an unsafe file reference")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
    except AudioError:
        raise
    except (TypeError, ValueError, KeyError):
        raise AudioError("UNSAFE_REPORT", "Audio report contains invalid data") from None
    if not isinstance(decoded, dict):
        raise AudioError("UNSAFE_REPORT", "Audio report is invalid")
    return decoded


def deserialize_result(payload: dict[str, Any], *, replay: bool = False) -> AudioBatchResult:
    """Rebuild the typed public result from a previously verified manifest.

    ``payload`` is decoded JSON; every field access below is guarded by the except clause.
    """
    try:
        if payload.get("schema_version") != "audio-batch-result.v1":
            raise ValueError

        def trim(value):
            return None if value is None else TrimRange(value.get("start_ms"), value.get("end_ms"))

        def targets(value):
            return LoudnessTargets(value["integrated_lufs"], value["true_peak_dbtp"], value["lra_lu"])

        def measurement(value):
            return (
                None
                if value is None
                else AudioMeasurement(
                    value["integrated_lufs"],
                    value["lra_lu"],
                    value["sample_peak_dbfs"],
                    value["true_peak_dbtp"],
                    value["duration_seconds"],
                )
            )

        tracks = []
        for item in payload["tracks"]:
            qa = item["qa"]
            error_data = item["error"]
            tracks.append(
                TrackResult(
                    track_id=item["track_id"],
                    status=item["status"],
                    source_relative=item["source_relative"],
                    source_sha256=item["source_sha256"],
                    trim=trim(item["trim"]),
                    requested_targets=targets(item["requested_targets"]),
                    pre_measurement=measurement(item["pre_measurement"]),
                    post_measurement=measurement(item["post_measurement"]),
                    output_format=item["output_format"],
                    output_relative_path=item["output_relative_path"],
                    output_sha256=item["output_sha256"],
                    output_bytes=item["output_bytes"],
                    normalization_type=item["normalization_type"],
                    qa=QAResult(bool(qa["passed"]), tuple(qa["failures"]), dict(qa["checks"])),
                    wall_seconds=item["wall_seconds"],
                    cpu_seconds=item["cpu_seconds"],
                    measurement_status=item["measurement_status"],
                    warnings=tuple(item["warnings"]),
                    error=None if error_data is None else ErrorInfo(error_data["code"], error_data["message"], error_data["retryable"]),
                )
            )
        return AudioBatchResult(
            schema_version="audio-batch-result.v1",
            batch_id=payload["batch_id"],
            status=payload["status"],
            recipe_version=payload["recipe_version"],
            fingerprint=payload["fingerprint"],
            ffmpeg_version=payload["ffmpeg_version"],
            ffprobe_version=payload["ffprobe_version"],
            model_calls=0,
            accepted_tracks=payload["accepted_tracks"],
            rejected_tracks=payload["rejected_tracks"],
            wall_seconds=payload["wall_seconds"],
            cpu_seconds=payload["cpu_seconds"],
            measurement_status=payload["measurement_status"],
            bytes_written=payload["bytes_written"],
            monetary_cost=payload["monetary_cost"],
            cost_status=payload["cost_status"],
            replay=replay,
            tracks=tuple(tracks),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AudioError("INVALID_MANIFEST", "Stored audio result manifest is invalid") from None
