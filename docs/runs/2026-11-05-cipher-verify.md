# 2026-11-05 — cipher verify against the live KOE parser

Checkpoint log. Append-only; the last section is always NEXT ACTION so a fresh session can
resume from this file alone.

## Setup (done)

- `trading-system-v2-SANDBOX` at `30b7cf7` (branch `claude/funny-wright-603voc`), read-only.
- `audio-agent-desktop` at `7bc1d6a` on `main`.
- `pip install -e ".[dev]"` FAILS: setuptools flat-layout discovery sees two top-level
  packages (`ui`, `audio_agent`). Pre-existing packaging defect, not in this task's scope;
  worked around by `pip install numpy sounddevice pytest` and running pytest from the repo root
  (pytest puts the root on `sys.path`). Baseline: `python -m pytest -q` → **69 passed**.

## Step 1 — DISCOVERY (pending)

## NEXT ACTION

Step 1: in trading-system-v2, find the KOE parse entry point the keystroke endpoint calls;
record file:line, call shape, return shape, verbatim, here.
