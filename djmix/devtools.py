"""開発用の検証（design_phase1.md §7.1 の同期 E2E 試験）。

画面側で 2 つのチャンネルを左右に分けて録音し（左=A、右=B）、ここで左右それぞれの拍を Beat This! で検出して、
B の各拍と最寄りの A の拍のずれを集計する。Phase 0 の追従試験と同じ評価方法。
両チャンネルに同じ検出器を使うので、検出器の癖（一定の遅れ）は打ち消し合う。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .analysis import beats as B
from .audio.decode import SR


def sync_check(data: bytes, k: float = 1.0, skip_sec: float = 3.0, windows: list[tuple[float, float]] | None = None) -> dict:
    """windows: 録音の中で両方のチャンネルが鳴っている区間（秒）。ミックスを通して再生した録音では、
    繋ぎの重なりの間だけを比べる（片方しか鳴っていない区間の拍は、遠くの拍と比べて大きなずれに見えてしまう）。"""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "rec.webm"
        src.write_bytes(data)
        proc = subprocess.run(creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), args=["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                              capture_output=True, check=True)
    st = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2)
    out = {"seconds": round(len(st) / SR, 1)}
    beats = {}
    for i, ch in enumerate("AB"):
        det = B.detect(np.ascontiguousarray(st[:, i]), SR)
        b = det["beats"]
        beats[ch] = b[b > skip_sec]
        out[f"beats_{ch}"] = int(len(beats[ch]))
    a, b = beats["A"], beats["B"]
    if windows:
        b = b[np.any([(b >= s0) & (b < s1) for s0, s1 in windows], axis=0)]
        out["beats_B_in_windows"] = int(len(b))
    if len(a) < 8 or len(b) < 8:
        return {**out, "error": "拍が少なすぎます"}
    # k=2 のとき B は A の倍の拍を刻むので、A の拍の間に中点を補ってから比べる
    if k == 2:
        a = np.sort(np.r_[a, (a[:-1] + a[1:]) / 2])
    elif k == 0.5:
        b = np.sort(np.r_[b, (b[:-1] + b[1:]) / 2])
    idx = np.clip(np.searchsorted(a, b), 1, len(a) - 1)
    near = np.where(np.abs(b - a[idx - 1]) < np.abs(b - a[idx]), a[idx - 1], a[idx])
    off = (b - near) * 1000
    # 検出器の倍テンポ誤り: 周りの拍間隔が曲の中央値の 0.7 倍未満の拍（間に余分な拍を検出している）は、
    # 同期のずれではないので分けて数える（P1-6 の 5 分試験で、テンポ伸縮後の Blue Train の 330〜370 秒付近を
    # 検出器が倍テンポで拾い、その 40 秒間だけ半拍ずれとして現れた。区間を過ぎるとずれは 0 に戻り、SYNC の保留も 0 回）
    def dense(x):
        ibi = np.diff(x)
        med = np.median(ibi)
        loc = np.minimum(np.r_[ibi[0], ibi], np.r_[ibi, ibi[-1]])
        return loc < 0.7 * med

    dense_b = dense(b)
    dense_a_near = dense(a)[np.clip(np.searchsorted(a, b), 0, len(a) - 1)]
    octave = dense_b | dense_a_near
    valid = off[~octave]
    med = float(np.median(valid))
    dev = np.abs(valid - med)
    bad = (~octave) & (np.abs(off) > 50)
    return {**out, "detector_octave_beats": int(octave.sum()),
            "offset_median_ms": round(med, 1), "jitter_p95_ms": round(float(np.percentile(dev, 95)), 1),
            "abs_p95_ms": round(float(np.percentile(np.abs(valid), 95)), 1),
            "offbeat_ratio_50ms": round(float(np.mean(np.abs(valid) > 50)), 3),
            "offbeat_times": np.round(b[bad], 1).tolist()[:50],
            "raw_offbeat_ratio_50ms": round(float(np.mean(np.abs(off) > 50)), 3),
            "offsets_ms": np.round(off, 1).tolist(), "b_times": np.round(b, 2).tolist()}
