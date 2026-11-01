"""Phase 4 モックアップ用のデータ（work/phase4/mock4_data.json）。4 曲を解析し（ユーザーの解析キャッシュには書かない）、
Demucs で分離して小節ごとのパートの量（ボーカル・ドラム・ベース・その他）を求める。形は Phase 2 のモックアップと同じ。"""
from __future__ import annotations

import json, sqlite3, sys
from pathlib import Path
import numpy as np, torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from djmix.analysis.pipeline import analyze_file  # noqa: E402
from djmix.analysis import sections as SEC  # noqa: E402
from djmix.audio.decode import SR, decode  # noqa: E402
from djmix.library.textfix import fix_mojibake  # noqa: E402
from demucs.pretrained import get_model  # noqa: E402
from demucs.apply import apply_model  # noqa: E402

IDS = [256, 232, 1602, 2988]      # LET'S DANCE BABY, CIRCUS TOWN（山下達郎）, Asayake, Night storm（Casiopea）
RATE = 100


def main():
    c = sqlite3.connect(f"file:{ROOT / 'data' / 'library.sqlite'}?mode=ro", uri=True)
    model = get_model("htdemucs").cuda().eval()
    out = []
    for tid in IDS:
        path, title, artist = c.execute("select path, title, artist from tracks where id=?", (tid,)).fetchone()
        a, wave, feats, _ = analyze_file(path)
        secs = SEC.compute(a, feats)
        x = decode(path).T.astype(np.float32)
        wav = torch.from_numpy(x); ref = wav.mean(0); m, s = ref.mean(), ref.std()
        with torch.no_grad():
            st = apply_model(model, ((wav - m) / s)[None].cuda(), device="cuda", shifts=0, split=True, overlap=0.25, progress=False)[0] * s + m
        st = st.mean(1).cpu().numpy()                          # (4, n) モノラル
        edges = [b["time"] for b in a["bars"]] + [a["duration"]]
        stems = {}
        for i, name in enumerate(model.sources):
            v = [float(np.sqrt(np.mean(st[i, int(t0 * SR):max(int(t0 * SR) + 1, int(t1 * SR))] ** 2))) for t0, t1 in zip(edges[:-1], edges[1:])]
            stems[name] = v
        mx = max(max(v) for v in stems.values()) or 1
        stems = {k: [round(x / mx, 3) for x in v] for k, v in stems.items()}
        w = wave[:, : wave.shape[1] // 2 * 2].reshape(3, -1, 2).max(axis=2)
        out.append({"id": tid, "title": fix_mojibake(title), "artist": fix_mojibake(artist), "duration": a["duration"], "bpm": a["bpm"], "meter": a["meter"],
                    "camelot": a["key"]["camelot"], "beats": np.round(a["beats"], 3).tolist(),
                    "bars": [[b["beat"], round(b["time"], 3)] for b in a["bars"]], "no_beat": a["no_beat"], "sections": secs,
                    "stems": stems, "wave": {"rate": RATE, "low": w[0].tolist(), "mid": w[1].tolist(), "high": w[2].tolist()}})
        vb = sum(1 for v in stems["vocals"] if v > 0.25)
        print(title, artist, round(a["bpm"], 1), a["key"]["camelot"], len(a["bars"]), "bars, vocal bars", vb)
    p = ROOT / "work" / "phase4" / "mock4_data.json"
    p.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(p, p.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
