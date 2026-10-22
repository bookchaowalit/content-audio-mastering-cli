"""Local path, target, and probe validation before audio processing."""

from __future__ import annotations

import hashlib
import math
import re
import time
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .errors import AudioError
from .models import AudioLimits, CommandRunner, TrackRequest, ValidatedTrack

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)


def _unsafe_source(source: str) -> bool:
    normalized = source.replace("\\", "/")
    parsed = urlsplit(source)
    return (
        not source.strip()
        or Path(source).is_absolute()
        or PureWindowsPath(source).is_absolute()
        or bool(parsed.scheme or parsed.netloc)
        or any(part == ".." for part in normalized.split("/"))
    )


def validate_roots(input_root: Path, output_root: Path) -> tuple[Path, Path]:
    """Resolve roots and prevent source/output trees from overlapping."""
    try:
        source = input_root.resolve(strict=True)
        output = output_root.resolve(strict=False)
    except OSError:
        raise AudioError("UNSAFE_PATH", "Input and output roots must be valid local directories") from None
    if not source.is_dir():
        raise AudioError("SOURCE_NOT_FOUND", "Input root must be an existing directory")
    if source == output or source in output.parents or output in source.parents:
        raise AudioError("UNSAFE_PATH", "Input and output roots must not overlap")
    return source, output


def validate_output_locations(output_root: Path, batch_id: str) -> None:
    """Reject redirected output descendants before any batch-controlled write."""
    root = Path(output_root).resolve(strict=False)
    locations = (
        ((".locks",), True),
        ((".locks", f"{batch_id}.lock"), False),
        ((".staging",), True),
        ((".staging", batch_id), True),
        ((batch_id,), True),
        ((batch_id, "artifacts"), True),
    )
    for parts, expect_directory in locations:
        current = root
        for index, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                raise AudioError("UNSAFE_PATH", "Output paths must not contain symbolic links")
            if not current.exists():
                continue
            try:
                resolved = current.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                raise AudioError("UNSAFE_PATH", "Output path must remain inside the selected output root") from None
            is_final = index == len(parts) - 1
            if (not is_final or expect_directory) and not current.is_dir():
                raise AudioError("OUTPUT_UNAVAILABLE", "Output directory path is not a directory")
            if is_final and not expect_directory and current.is_dir():
                raise AudioError("OUTPUT_UNAVAILABLE", "Output lock path is not a file")


def validate_track(
    track: TrackRequest,
    input_root: Path,
    limits: AudioLimits,
    runner: CommandRunner,
    *,
    toolchain=None,
    deadline: float | None = None,
) -> ValidatedTrack:
    """Validate one track, then inspect the media through the bounded probe adapter."""
    from .limits import validate_limits

    validate_limits(limits)
    if not isinstance(track.track_id, str) or _IDENTIFIER.fullmatch(track.track_id) is None:
        raise AudioError("INVALID_TRACK_ID", "Track identifiers must contain safe ASCII characters")
    if not isinstance(track.source, str) or _unsafe_source(track.source):
        raise AudioError("UNSAFE_PATH", "Source must be a relative local file within the input root")
    if not isinstance(track.output_format, str) or track.output_format not in {"flac", "wav_pcm24"}:
        raise AudioError("INVALID_OUTPUT_FORMAT", "Output format must be flac or wav_pcm24")

    targets = track.targets
    numeric_targets: tuple[float, ...] = (targets.integrated_lufs, targets.true_peak_dbtp)
    if targets.lra_lu is not None:
        numeric_targets += (targets.lra_lu,)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in numeric_targets):
        raise AudioError("INVALID_TARGET", "Loudness targets must be finite numbers")
    if not -70 <= targets.integrated_lufs <= -5 or not -9 <= targets.true_peak_dbtp <= 0:
        raise AudioError("INVALID_TARGET", "Loudness or true-peak target is outside the supported range")
    if targets.lra_lu is not None and not 1 <= targets.lra_lu <= 50:
        raise AudioError("INVALID_TARGET", "Loudness-range target is outside the supported range")

    trim = track.trim
    start_ms = 0 if trim is None or trim.start_ms is None else trim.start_ms
    end_ms = None if trim is None else trim.end_ms
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or start_ms < 0
        or (end_ms is not None and (isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms <= start_ms))
    ):
        raise AudioError("INVALID_TRIM", "Trim end must be greater than a non-negative trim start")

    try:
        root = input_root.resolve(strict=True)
    except OSError:
        raise AudioError("SOURCE_NOT_FOUND", "Input root is unavailable") from None
    if not root.is_dir():
        raise AudioError("SOURCE_NOT_FOUND", "Input root must be an existing directory")
    try:
        source_path = (root / track.source).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AudioError("SOURCE_NOT_FOUND", "Source audio file was not found") from None
    try:
        source_path.relative_to(root)
    except ValueError:
        raise AudioError("UNSAFE_PATH", "Source must remain inside the input root") from None
    if not source_path.is_file():
        raise AudioError("SOURCE_NOT_FOUND", "Source audio file was not found")

    try:
        source_bytes = source_path.stat().st_size
    except OSError:
        raise AudioError("SOURCE_NOT_FOUND", "Source audio file cannot be read") from None
    if source_bytes <= 0 or source_bytes > limits.max_source_bytes:
        raise AudioError("RESOURCE_LIMIT_EXCEEDED", "Source file is empty or exceeds its size limit")

    from .probe import input_demuxer_for_path, probe_audio

    input_demuxer_for_path(source_path)

    def bounded_runner(argv, timeout_seconds, output_limit_bytes):
        remaining = limits.track_deadline_seconds if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise AudioError("PROCESS_TIMEOUT", "Audio track exceeded its time limit", retryable=True)
        return runner(
            argv,
            min(timeout_seconds, limits.track_deadline_seconds, remaining),
            min(output_limit_bytes, limits.process_output_limit_bytes),
        )

    probe = probe_audio(source_path, bounded_runner) if toolchain is None else probe_audio(source_path, bounded_runner, toolchain=toolchain)
    if probe.channels not in (1, 2) or probe.sample_rate <= 0:
        raise AudioError("UNSUPPORTED_STREAM", "Only mono or stereo audio streams are supported")
    if not math.isfinite(probe.duration_seconds) or probe.duration_seconds <= 0:
        raise AudioError("UNSUPPORTED_STREAM", "Audio duration could not be measured")
    if probe.duration_seconds > limits.max_track_seconds:
        raise AudioError("RESOURCE_LIMIT_EXCEEDED", "Source duration exceeds its track limit")

    start_sample = round(start_ms * probe.sample_rate / 1000)
    end_sample = None if end_ms is None else round(end_ms * probe.sample_rate / 1000)
    duration_samples = round(probe.duration_seconds * probe.sample_rate)
    if start_sample >= duration_samples or (end_sample is not None and end_sample > duration_samples):
        raise AudioError("INVALID_TRIM", "Trim range extends beyond the source duration")

    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                if deadline is not None and time.monotonic() >= deadline:
                    raise AudioError("PROCESS_TIMEOUT", "Audio track exceeded its time limit", retryable=True)
                digest.update(chunk)
    except AudioError:
        raise
    except OSError:
        raise AudioError("SOURCE_NOT_FOUND", "Source audio file cannot be read") from None

    return ValidatedTrack(
        request=track,
        source_path=source_path,
        source_sha256=digest.hexdigest(),
        trim_start_sample=start_sample,
        trim_end_sample=end_sample,
        probe=probe,
        limits=limits,
    )
