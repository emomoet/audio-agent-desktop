# Headset pickup test

Decides whether the headset mic can serve as the broker channel. ~1 hour on the trader PC.

## Setup

```
REM RUN ON THE WINDOWS DESKTOP
pip install sounddevice numpy
python tools\headset_capture_test.py --list
```
Pick the headset **input** device index. Put the voice software on a live call (headset DSP
behaves differently idle vs in-call). Play recorded pit noise from a speaker at floor level.

## Script

Write 20–30 real orders (one per line) and the matching `expected.jsonl` (see
`tools/expected.example.jsonl`). Someone reads them from broker distance (3–4 ft, off-axis),
pausing ~2 s between orders; the trader stays silent.

## Takes (90 s each)

```
REM RUN ON THE WINDOWS DESKTOP
python tools\headset_capture_test.py --device N --seconds 90 --out takeA.wav
```
- **A** unmuted, normal position
- **B** muted with the headset's own button — if the meter pins at −120 dBFS, mute kills the PC stream
- **C** unmuted, trader interjects ("working", "2.10 bid") between orders

If the broker is inaudible to a human on take A, stop: use an outward mic.

## Score

```
REM RUN ON THE WINDOWS DESKTOP
pip install faster-whisper
python tools\score_take.py takeA.wav expected.jsonl --config config.toml
```
Prints each segment's transcript, per-field accuracy, and the verdict:

| Field accuracy | Ruling |
|---|---|
| ≥ 90 % | headset mic for replay and extraction |
| 60–90 % | headset for replay; outward mic for extraction |
| < 60 %, or mute kills the stream | outward mic for both |

Record the headset model, dongle vs Bluetooth, mute behaviour, and the scores in
`SOURCES.md` in this repo.
