# 01 — Business process (As-Is / To-Be)

content-audio-mastering-cli (`book-audio`) คือ CLI ในเครื่องที่เตรียมไฟล์เสียงเป็น batch: ตัดตาม sample, ปรับ loudness ด้วย FFmpeg, ตรวจ QA เชิงตัวเลข แล้วเขียน FLAC / WAV 24-bit พร้อม `result.json` ที่ตรวจ hash ได้ ใช้โดย owner เพื่องานเสียงพูด/podcast ไม่มี AI และไม่มี hosted runtime

Last reviewed: 2026-11-28 · commit `06da65b`

## As-Is (จากโค้ดวันนี้)

```mermaid
flowchart TD
  OP["owner: book-audio run --manifest batch.json"] --> CLI["cli.py main"]
  CLI --> VAL["validation.py: audio-batch.v1 (<= 1 MiB, track id ไม่ซ้ำแบบ case-insensitive)"]
  VAL -->|invalid| EX2["exit 2"]
  VAL --> LIM["limits.py + config.py: hard limits (local config ลดได้ ห้ามเพิ่ม)"]
  LIM --> TOOL["probe.py: ตรวจ ffmpeg/ffprobe ตรง toolchain.lock.json"]
  TOOL --> FP["batch.py fingerprint_sources: SHA-256 ของ input + settings + toolchain"]
  FP --> REPLAY{"มี result เดิมของ batch_id?"}
  REPLAY -->|fingerprint ตรง| SAVED["คืน result เดิม ไม่รัน FFmpeg (exit 0)"]
  REPLAY -->|fingerprint ต่าง| CONFLICT["REPLAY_CONFLICT"]
  REPLAY -->|ไม่มี| LOCK["locking.py: lock ต่อ batch (lock ค้างจาก crash เก็บไว้ให้ตรวจเอง)"]
  LOCK --> STAGE["ทิ้ง .staging เก่า แล้วสร้าง staging ใหม่"]
  STAGE --> RENDER["engine.py render_track: probe, trim, loudnorm, เขียน FLAC/WAV"]
  RENDER --> QA["qa.py + measurements.py: LUFS, true peak, LRA, duration, format"]
  QA --> ART["artifacts.py + manifest.py: result.json + .sha256 (ไม่มี raw media / absolute path)"]
  ART --> PUB["publish batch directory แบบ atomic (มีอยู่แล้ว = OUTPUT_EXISTS)"]
  PUB --> EXIT["exit 0 สำเร็จ, 2 reject ทั้ง batch, 3 partial, 4 fatal"]
  EXIT --> VERIFY["book-audio verify: ตรวจ digest + ขนาด/hash ของทุก output"]
```

Sources: `src/book_audio_production/cli.py`, `validation.py`, `limits.py`, `config.py`, `probe.py`, `toolchain_lock.py`, `batch.py` (`fingerprint_sources`, `process_batch_once`, `discard_incomplete_staging`, `_publish_batch_directory`), `engine.py` (`render_track`, `process_batch`), `qa.py`, `artifacts.py`, `manifest.py`, `replay.py`, `README.md`, `PRODUCT.md`

ข้อเท็จจริงสำคัญจากโค้ด:

- ไม่มีการเรียก LLM / transcription: ทุก result รายงาน `model_calls: 0`; ต้นทุนเป็นเงินรายงาน unknown เพราะไม่มี rate card
- FFmpeg/FFprobe ที่ pin เป็น Windows build: บน Linux CI test ที่ render เสียงจริงจะ skip (`.github/workflows/ci.yml`, coverage gate 58%)
- `book-audio benchmark` สร้าง tone สังเคราะห์ในเครื่องเพื่อวัดเวลา/ไบต์/replay (ไม่อยู่ในภาพ)

## To-Be (docs/UPGRADE-PLAN.md §Backlog; PRODUCT.md §Limits)

To-Be ที่เขียนไว้เป็นงานเสริมของกระบวนการเดิม ไม่ใช่กระบวนการใหม่:

```mermaid
flowchart TD
  OP["owner: book-audio run"] --> CLI["cli.py main"]
  OP --> DOCTOR["book-audio doctor: รายงานสถานะ toolchain และ mismatch ที่ชัด (P2)"]
  CLI --> VAL["validation.py"]
  VAL --> TOOL["probe.py: ตรวจ toolchain"]
  TOOL --> RENDER["render + QA + manifest + publish (เหมือนเดิม)"]
  RENDER --> LISTEN["human listening comparison ของตัวอย่างเสียงพูด ก่อนเรียก pilot-ready"]
  CI["CI windows-latest: ดาวน์โหลด FFmpeg 9.0.1 ที่ pin (ตรวจ checksum) แล้วรัน real-audio tests (P1)"] -.-> RENDER
```

Sources: `docs/UPGRADE-PLAN.md` §Backlog P0–P2, `PRODUCT.md` §Limits, `docs/engineering/2026-09-26-audio-listening-checklist.md`

## Gap

| ID | As-Is → To-Be | Status | Owner | Evidence |
|---|---|---|---|---|
| G1 | ยังไม่ยืนยันว่า GitHub Actions run แรกเขียว → CI เขียว | todo | claude | `docs/UPGRADE-PLAN.md` §Backlog P0 |
| G2 | real-audio tests skip บน Linux CI → job `windows-latest` ดาวน์โหลด FFmpeg ที่ pin แล้วรัน | todo | claude | `docs/UPGRADE-PLAN.md` §Backlog P1; `.github/workflows/ci.yml` |
| G3 | `batch.py` ยังไม่มี type annotations ครบ → strict mypy + Ruff `PTH`, `RET`, `C4` | todo | claude | `docs/UPGRADE-PLAN.md` §Backlog P1 |
| G4 | toolchain mismatch แสดงแค่ error code → คำสั่ง `book-audio doctor` | todo | claude | `docs/UPGRADE-PLAN.md` §Backlog P2 |
| G5 | QA เป็นตัวเลขอย่างเดียว → human listening comparison ก่อนเรียก pilot-ready | todo | owner | `PRODUCT.md` §Limits; `docs/engineering/2026-09-26-audio-listening-checklist.md` |
| G6 | tier และ lifecycle ใน roadmap `ai-labs` เป็น unknown → owner ตัดสิน lab หรือ product | decide | owner | `PRODUCT.md` §Disposition |
| G7 | ownership ระหว่าง CLI นี้กับ platform `audio_production` ไม่ชัด → แยก `audio-batch.v1` กับ `audio-ir.v1` | done | claude | `docs/UPGRADE-PLAN.md` §Done in this pass (pass 3); `README.md` §Ownership boundary |

## Open questions

- ผล GitHub Actions run ล่าสุดบน main [ต้องยืนยัน] (UPGRADE-PLAN ยังมี P0 ค้าง)
- ต้องการ rate card ในเครื่องเพื่อรายงานต้นทุนเป็นเงินหรือไม่ (ตอนนี้เป็น unknown เสมอ)
