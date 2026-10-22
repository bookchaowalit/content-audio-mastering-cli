# Book Audio Production (content-audio-mastering-cli)

## Purpose

A local command-line tool that prepares batches of audio files: it trims each
track by sample position, normalizes loudness with FFmpeg, checks the rendered
master against numeric QA rules, and writes lossless FLAC or 24-bit PCM WAV
outputs plus a hash-verified `result.json` manifest.

## Users

Intended for podcast and spoken-audio producers who need repeatable loudness
targets. No confirmed external users today; it is used by the owner.

## What works today

- `book-audio run` processes an `audio-batch.v1` manifest (`schemas/`): up to 20
  tracks, per-track trim, integrated LUFS / true-peak / optional LRA targets,
  FLAC or `wav_pcm24` output.
- `book-audio verify` checks the manifest digest and every accepted output's size
  and SHA-256.
- Exact replay: the same batch, inputs, settings and pinned toolchain return the
  saved result without running FFmpeg; any change returns `REPLAY_CONFLICT`.
  Existing results are never overwritten; batches publish atomically from a
  staging directory.
- `book-audio benchmark` runs a synthetic local benchmark (tones only).
- Hard resource limits (size, duration, output bytes, deadlines) that a local
  config can lower but not raise.

## How to run

```bash
uv sync --frozen --all-extras
uv run book-audio --help
uv run book-audio run --manifest batch.json --input-root audio-in --output-root audio-out --json
uv run book-audio verify audio-out/<batch_id>/result.json
```

Checks (same as CI): `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy`, `uv run python -m pytest -q --cov`.

## AI

None. The tool makes no LLM, transcription or generative-audio calls; every
result reports `model_calls: 0`. Audio never leaves the machine.

## Limits

- The FFmpeg/FFprobe build pinned in `src/book_audio_production/toolchain.lock.json`
  is a Windows build (gyan.dev). On other hosts, including Linux CI, the tests that
  render or replay real audio skip; CI covers validation, manifests, artifacts,
  replay logic and the CLI (coverage gate 58%).
- QA is numeric only (loudness, peak, trim, format). A human listening comparison
  of a speech sample is still pending, so it is not called pilot-ready.
- Monetary cost is always reported as unknown (no local rate card).
- A lock left by a crashed process is kept for manual inspection.

## Live URL

None. This is a local CLI with no hosted runtime, so there is no deploy and no
Sentry wiring.

## Disposition

Lab tool, kept public. Tier and lifecycle in the `ai-labs` roadmap entry are still
`unknown`; deciding lab vs product is an owner decision.
