import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from book_audio_production.artifacts import verify_manifest
from book_audio_production.batch import _publish_batch_directory
from book_audio_production.engine import process_batch
from book_audio_production.errors import AudioError
from book_audio_production.models import (
    AudioBatch,
    AudioLimits,
    LoudnessTargets,
    TrackRequest,
)
from book_audio_production.report import serialize_result
from tests.audio_fixtures import create_synthetic_audio
from tests.toolchain import requires_pinned_toolchain


def test_batch_directory_publish_retries_transient_os_errors(tmp_path: Path, monkeypatch) -> None:
    staged = tmp_path / ".staged"
    destination = tmp_path / "batch"
    staged.mkdir()
    (staged / "result.json").write_text("complete", encoding="utf-8")
    original_rename = os.rename
    attempts = 0

    def fail_once(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary directory publication error")
        return original_rename(source, target)

    monkeypatch.setattr("book_audio_production.batch.os.rename", fail_once)

    _publish_batch_directory(staged, destination, deadline=time.monotonic() + 2)

    assert attempts == 2
    assert (destination / "result.json").read_text(encoding="utf-8") == "complete"
    assert not staged.exists()


def test_batch_directory_publish_does_not_overwrite_existing_output(tmp_path: Path) -> None:
    staged = tmp_path / ".staged"
    destination = tmp_path / "batch"
    staged.mkdir()
    destination.mkdir()
    (destination / "keep.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(AudioError) as error:
        _publish_batch_directory(staged, destination, deadline=time.monotonic() + 2)

    assert error.value.code == "OUTPUT_EXISTS"
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "existing"


@requires_pinned_toolchain
def test_process_batch_publishes_manifest_and_keeps_per_track_failure_local(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    create_synthetic_audio(input_root / "tone.wav", duration=0.8)
    batch = AudioBatch(
        "batch-1",
        (
            TrackRequest("tone", "tone.wav", None, LoudnessTargets(-18, -1, None), "flac"),
            TrackRequest("missing", "missing.wav", None, LoudnessTargets(-18, -1, None), "flac"),
        ),
    )

    result = process_batch(batch, input_root, output_root, AudioLimits())
    manifest_path = output_root / "batch-1" / "result.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "audio-batch-result.v1.schema.json").read_text())

    assert result.status == "completed_with_errors"
    assert result.accepted_tracks == result.rejected_tracks == 1
    assert result.model_calls == 0
    assert result.monetary_cost is None
    assert result.tracks[0].output_relative_path is not None
    assert result.tracks[1].error is not None
    assert result.tracks[1].error.code == "SOURCE_NOT_FOUND"
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
    assert verify_manifest(manifest_path).passed is True
    serialized_tracks: Any = serialize_result(result)["tracks"]
    assert "output" not in serialized_tracks[0]["output_relative_path"]


@requires_pinned_toolchain
def test_process_batch_publishes_sanitized_rejection_without_audio_outputs(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    batch = AudioBatch(
        "rejected-1",
        (TrackRequest("unsafe", "../private.wav", None, LoudnessTargets(-18, -1, None), "flac"),),
    )

    result = process_batch(batch, input_root, output_root, AudioLimits())
    manifest_path = output_root / "rejected-1" / "result.json"
    payload_text = manifest_path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)

    assert result.status == "rejected"
    assert result.tracks[0].error is not None
    assert result.tracks[0].error.code == "UNSAFE_PATH"
    assert payload["tracks"][0]["source_relative"] is None
    assert "private" not in payload_text
    assert payload["tracks"][0]["output_relative_path"] is None
    assert verify_manifest(manifest_path).passed is True


def test_batch_directory_publish_does_not_replace_existing_empty_output(tmp_path: Path) -> None:
    staged = tmp_path / ".staged"
    destination = tmp_path / "batch"
    staged.mkdir()
    (staged / "result.json").write_text("{}", encoding="utf-8")
    destination.mkdir()

    with pytest.raises(AudioError) as error:
        _publish_batch_directory(staged, destination, deadline=time.monotonic() + 2)

    assert error.value.code == "OUTPUT_EXISTS"
    assert list(destination.iterdir()) == []
    assert (staged / "result.json").is_file()


def test_batch_directory_publish_moves_staged_batch_into_place(tmp_path: Path) -> None:
    staged = tmp_path / ".staged"
    destination = tmp_path / "batch"
    staged.mkdir()
    (staged / "result.json").write_text("{}", encoding="utf-8")

    _publish_batch_directory(staged, destination, deadline=time.monotonic() + 2)

    assert (destination / "result.json").read_text(encoding="utf-8") == "{}"
    assert not staged.exists()
