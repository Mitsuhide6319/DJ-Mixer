"""Phase 2 モックアップ用のデータ（work/phase2/mock2_data.json）。ユーザーのキューにある曲の実際の解析結果と区間候補。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from djmix import db  # noqa: E402
from djmix.analysis import cache  # noqa: E402
from sections_proto import sections  # noqa: E402

TRACKS = [1602, 1596, 2988, 1674, 1537, 1621]   # Asayake, Living On A Feeling, Night storm, Magic Ray, Fightman, Misty Lady
RATE = 100


def main():
    c = db.connect()
    out = []
    for tid in TRACKS:
        r = dict(c.execute("SELECT id, title, artist, duration, bpm, camelot, beat_class FROM tracks WHERE id = ?", (tid,)).fetchone())
        a, segs = sections(tid)
        w = np.fromfile(cache.track_dir(tid) / "wave.u8", dtype=np.uint8).reshape(3, -1)
        n = w.shape[1] // 2
        w = w[:, : n * 2].reshape(3, n, 2).max(axis=2)      # 200 点/秒 → 100 点/秒（最大値で間引く）
        out.append({
            **r, "duration": a["duration"], "bpm": a["bpm"], "meter": a["meter"],
            "beats": np.round(a["beats"], 3).tolist(),
            "bars": [[b["beat"], round(b["time"], 3)] for b in a["bars"]],
            "no_beat": a["no_beat"], "sections": segs,
            "wave": {"rate": RATE, "low": w[0].tolist(), "mid": w[1].tolist(), "high": w[2].tolist()},
        })
    p = ROOT / "work" / "phase2" / "mock2_data.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(p, p.stat().st_size // 1024, "KB", [t["title"] for t in out])


if __name__ == "__main__":
    main()
