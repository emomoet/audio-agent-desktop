"""Score a recorded take: segmentation + ASR + extraction, field by field.

  python tools/score_take.py take01.wav expected.jsonl [--config config.toml] [--model medium.en]
  python tools/score_take.py --transcripts lines.txt expected.jsonl     # extraction only, no audio

expected.jsonl: one JSON object per order, in the order they were read, e.g.
  {"ticker": "AAPL", "expiries": [{"month": 5}], "strikes": [180, 190], "cp": "c",
   "spread_type": "cs", "broker_side": "b", "broker_qty": 500, "price": 2.1}
Only the keys present are scored. Field accuracy (not WER) is the number that decides
the headset-vs-outward-mic ruling (>=90% / 60-90% / <60%).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402

from audio_agent.asr import AsrResult, make_asr  # noqa: E402
from audio_agent.config import AsrCfg, ExtractCfg, SegmenterCfg, load  # noqa: E402
from audio_agent.extract import Extractor  # noqa: E402
from audio_agent.ringbuffer import RATE, RingBuffer  # noqa: E402
from audio_agent.segmenter import Segmenter  # noqa: E402
from audio_agent.sources.file import read_wav  # noqa: E402
from audio_agent.dsp import Resampler  # noqa: E402

FIELDS = ["ticker", "expiries", "strikes", "cp", "spread_type", "ratio", "broker_side", "broker_qty", "price"]


def norm(name, v):
    if v is None:
        return None
    if name == "expiries":
        return [(e["month"], e.get("day"), e.get("year")) for e in v]
    if name in ("strikes", "ratio"):
        return [float(x) for x in v]
    if name in ("price",):
        return round(float(v), 2)
    return v


def segments_from_wav(path: str, seg_cfg: SegmenterCfg):
    data, rate = read_wav(path)
    audio = Resampler(rate, RATE).process(data)
    buf = RingBuffer(audio.size / RATE + 5)
    segs = []
    seg = Segmenter(seg_cfg, segs.append)
    block = 320
    for i in range(0, audio.size, block):
        chunk = audio[i:i + block]
        buf.write(chunk, t_last=(i + chunk.size) / RATE)
        seg.feed(chunk, buf.written, (i + chunk.size) / RATE)
    tail = np.zeros(int(RATE * 1.5), dtype=np.float32)    # flush a final segment
    buf.write(tail, t_last=audio.size / RATE + 1.5)
    seg.feed(tail, buf.written, audio.size / RATE + 1.5)
    return [(s, buf.read_range(s.start_idx, s.end_idx)) for s in segs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?")
    ap.add_argument("expected")
    ap.add_argument("--transcripts", help="text file, one transcript per line (skips audio + ASR)")
    ap.add_argument("--config")
    ap.add_argument("--model", default="medium.en")
    ap.add_argument("--min-conf", type=float, default=None)
    a = ap.parse_args()

    cfg = load(a.config) if a.config else None
    aliases = cfg.symbol_aliases if cfg else {}
    ex_cfg = cfg.extract if cfg else ExtractCfg()
    if a.min_conf is not None:
        ex_cfg = ExtractCfg(**{**ex_cfg.__dict__, "min_field_conf": a.min_conf})
    extractor = Extractor(ex_cfg, aliases)

    results: list[tuple[str, dict | None]] = []
    if a.transcripts:
        for line in open(a.transcripts, encoding="utf-8"):
            if line.strip():
                p = extractor.run(AsrResult(line.strip(), 1.0), {})
                results.append((line.strip(), p.fields.to_dict() if p else None))
    else:
        asr_cfg = cfg.asr if cfg else AsrCfg(model=a.model)
        asr = make_asr(asr_cfg, sorted(set(aliases.values())))
        segs = segments_from_wav(a.wav, cfg.segmenter if cfg else SegmenterCfg())
        print(f"{len(segs)} segments found")
        for s, audio in segs:
            r = asr.transcribe(audio)
            p = extractor.run(r, {"id": s.id})
            print(f"  seg {s.id:3d} {s.duration_s:5.1f}s conf {r.confidence:.2f}: {r.text}")
            results.append((r.text, p.fields.to_dict() if p else None))

    expected = [json.loads(line) for line in open(a.expected, encoding="utf-8") if line.strip()]
    print(f"\n{len(expected)} expected orders, {sum(1 for _, f in results if f)} proposals\n")
    hit = {f: 0 for f in FIELDS}
    tot = {f: 0 for f in FIELDS}
    for i, exp in enumerate(expected):
        got = results[i][1] if i < len(results) else None
        wrong = []
        for f in FIELDS:
            if f not in exp:
                continue
            tot[f] += 1
            gv = norm(f, got[f]["value"]) if got else None
            if gv == norm(f, exp[f]):
                hit[f] += 1
            else:
                wrong.append(f"{f}: want {exp[f]!r} got {gv!r}")
        heard = results[i][0] if i < len(results) else "<no segment>"
        print(f"#{i + 1:3d} {'OK ' if not wrong else 'ERR'} {heard}")
        for w in wrong:
            print(f"        {w}")
    print("\nfield accuracy")
    all_hit = all_tot = 0
    for f in FIELDS:
        if tot[f]:
            print(f"  {f:12s} {hit[f]:4d}/{tot[f]:<4d} {100 * hit[f] / tot[f]:5.1f}%")
            all_hit += hit[f]
            all_tot += tot[f]
    if all_tot:
        pct = 100 * all_hit / all_tot
        verdict = ("headset mic for both" if pct >= 90 else
                   "headset for replay, outward mic for extraction" if pct >= 60 else
                   "outward mic for both")
        print(f"  {'OVERALL':12s} {all_hit:4d}/{all_tot:<4d} {pct:5.1f}%   -> {verdict}")


if __name__ == "__main__":
    main()
