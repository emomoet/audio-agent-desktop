"""
Headset mic pickup test — records the headset's Windows input device raw to WAV.

Usage:
  pip install sounddevice numpy
  python headset_capture_test.py --list                 # find the headset's input device index
  python headset_capture_test.py --device 3 --seconds 90 --out take01.wav

While recording, the script prints a running RMS level (dBFS) once per second so you can see
live whether the broker's voice is registering at all, and whether MUTE zeroes the stream.
"""
import argparse, math, sys, time, wave
import numpy as np
import sounddevice as sd

RATE = 16000      # ASR-native rate; headset DSP output is rarely better than this anyway
CHANNELS = 1

def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"{i:3d}  {d['name']}  (in={d['max_input_channels']}, {d['hostapi']})")

def dbfs(block):
    rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
    return -120.0 if rms == 0 else 20 * math.log10(rms)

def record(device, seconds, out):
    frames = []
    last_print = time.time()
    level_buf = []

    def cb(indata, nframes, t, status):
        nonlocal last_print, level_buf
        if status:
            print(f"[status] {status}", file=sys.stderr)
        frames.append(indata.copy())
        level_buf.append(indata.copy())
        now = time.time()
        if now - last_print >= 1.0:
            lvl = dbfs(np.concatenate(level_buf))
            bar = "#" * max(0, int((lvl + 60) / 2))
            print(f"{now - t0:6.1f}s  {lvl:7.1f} dBFS  {bar}")
            level_buf = []
            last_print = now

    t0 = time.time()
    with sd.InputStream(device=device, samplerate=RATE, channels=CHANNELS,
                        dtype="float32", callback=cb, blocksize=1600):
        print(f"Recording {seconds}s from device {device} -> {out}")
        sd.sleep(int(seconds * 1000))
    audio = np.concatenate(frames)
    pcm = (np.clip(audio.reshape(-1), -1, 1) * 32767).astype("<i2")
    with wave.open(out, "wb") as w:          # stdlib WAV writer: no soundfile dependency
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm.tobytes())
    print(f"Saved {out}: {len(audio)/RATE:.1f}s, peak {dbfs(audio):.1f} dBFS")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    p.add_argument("--device", type=int)
    p.add_argument("--seconds", type=float, default=90)
    p.add_argument("--out", default="take.wav")
    a = p.parse_args()
    if a.list:
        list_devices()
    elif a.device is None:
        p.error("--device required (run --list first)")
    else:
        record(a.device, a.seconds, a.out)
