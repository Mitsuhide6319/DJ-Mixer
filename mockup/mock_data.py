"""UIモックアップ用に、実曲の3帯域波形と拍・小節データを書き出す（work/mock_data.json）。"""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase0"))
from analyze import MUSIC, OUT, SR, decode, local_tempo  # noqa: E402

RATE = 200  # 波形点/秒（拡大表示で階段状にならないよう 100→200 に上げた）


def bands(mono):
    lo = sosfilt(butter(4, 200, "lowpass", fs=SR, output="sos"), mono)
    mid = sosfilt(butter(4, [200, 2500], "bandpass", fs=SR, output="sos"), mono)
    hi = sosfilt(butter(4, 2500, "highpass", fs=SR, output="sos"), mono)
    hop = SR // RATE
    res = []
    for x in (lo, mid, hi):
        n = len(x) // hop
        p = np.abs(x[: n * hop]).reshape(n, hop).max(axis=1)
        res.append(p)
    m = max(r.max() for r in res)
    return [np.clip(r / m * 255, 0, 255).astype(int) for r in res]


out = {}
for tid, title, artist in (("jz_bluetrain", "Blue Train", "John Coltrane"), ("jz_sowhat", "So What", "Miles Davis")):
    r = json.loads((OUT / tid / "result.json").read_text(encoding="utf-8"))
    a = decode(MUSIC / r["path"])
    lo, mid, hi = bands(a.mean(axis=1))
    beats = np.array(r["beat_times"])
    # 表示用に平滑化した拍時刻（設計 §3.3 の簡易版）
    idx = np.arange(len(beats))
    sm = beats.copy()
    for i in range(len(beats)):
        s, e = max(0, i - 4), min(len(beats), i + 5)
        p = np.polyfit(idx[s:e], beats[s:e], 1)
        v = np.polyval(p, i)
        if abs(v - beats[i]) < 0.03:
            sm[i] = v
    down = np.array(r["downbeat_times"])
    bar_beats = np.searchsorted(beats, down - 1e-3).tolist()
    ov = 1200
    def overview(x):
        n = len(x) // ov
        return x[: n * ov].reshape(ov, n).max(axis=1).tolist()
    out[tid] = {
        "title": title, "artist": artist, "duration": r["duration"],
        "bpm": r["tempo"]["bpm_median"], "camelot": r["key"]["camelot"], "lufs": r["lufs"],
        "beats": np.round(sm, 4).tolist(), "barBeats": bar_beats,
        "noBeat": r["no_beat_regions"],
        "wave": {"rate": RATE, "low": lo.tolist(), "mid": mid.tolist(), "high": hi.tolist()},
        "overview": {"low": overview(lo), "mid": overview(mid), "high": overview(hi)},
    }
(Path(OUT).parent / "mock_data.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
print({k: (len(v["beats"]), v["bpm"]) for k, v in out.items()}, (Path(OUT).parent / "mock_data.json").stat().st_size // 1024, "KB")
