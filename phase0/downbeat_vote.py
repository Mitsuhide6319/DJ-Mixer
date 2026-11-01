"""小節頭の揺らぎが「余分な小節頭の混入」なのか「位相そのものの誤り」なのかを調べる。
主拍子 M を仮定し、16小節分の窓で『拍番号 mod M』の多数決を取って位相を決め、
検出された小節頭のうち多数決位相と一致する割合を出す。"""
import json
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "work" / "phase0"
for r in json.loads((OUT / "classified.json").read_text(encoding="utf-8")):
    if r["class"] == "C":
        continue
    full = json.loads((OUT / r["id"] / "result.json").read_text(encoding="utf-8"))
    beats, downs = np.array(full["beat_times"]), np.array(full["downbeat_times"])
    M = r["bars"]["dominant_meter"]
    if M < 3:
        M = 4
    bi = np.searchsorted(beats, downs - 1e-3)
    agree, phase_changes, prev = 0, 0, None
    for k, b in enumerate(bi):
        win = bi[max(0, k - 8): k + 8]
        phase = np.bincount(win % M, minlength=M).argmax()
        if prev is not None and phase != prev:
            phase_changes += 1
        prev = phase
        agree += int(b % M == phase)
    print(f"{r['class']} {r['id']:16s} M={M} 検出小節頭が多数決位相と一致: {agree / len(bi):5.0%}  "
          f"元の主拍子率: {r['bars']['dominant_ratio']:.0%}  位相の切替わり: {phase_changes}回")
