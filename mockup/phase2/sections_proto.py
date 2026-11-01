"""Phase 2 企画: 曲の区間候補（構造の区切り）を、解析キャッシュの小節ごとの特徴量から求める試作。

小節ごとの特徴量（音量・低音・和音の濃さ・クロマ）の変化が大きい小節を区切りにし、
区間ごとに「静か / 中くらい / 盛り上がり」「低音少なめ」「拍なし」などの性格を付ける。
結果をグラフにして、実曲の構造と合っているかを目で確かめる（work/phase2/sections_*.png）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from djmix.analysis import cache  # noqa: E402
from djmix.analysis.features import BAR_COLUMNS  # noqa: E402

OUT = ROOT / "work" / "phase2"


def sections(track_id: int, win: int = 8, min_len: int = 8) -> tuple[dict, list[dict]]:
    a = cache.read(track_id)
    feats = cache.bars_array(track_id, len(BAR_COLUMNS))
    bars = a["bars"]
    n = len(bars)
    rms, low, harm = feats[:, 0], feats[:, 1], feats[:, -1]
    chroma = feats[:, 2:14]
    chroma = chroma / (np.linalg.norm(chroma, axis=1, keepdims=True) + 1e-9)
    # 特徴量を曲内で標準化して並べる（クロマは重みを下げる）
    def z(x):
        return (x - x.mean()) / (x.std() + 1e-9)
    X = np.column_stack([z(rms) * 1.5, z(low) * 1.2, z(harm), chroma * 1.0])
    # 前後 win 小節の平均の差（ノベルティ）
    nov = np.zeros(n)
    for i in range(win, n - win + 1):
        nov[i] = np.linalg.norm(X[i - win:i].mean(0) - X[i:i + win].mean(0))
    # 4 小節の区切りを少し優遇する（フレーズの頭になりやすい）
    bonus = np.array([1.08 if i % 4 == 0 else 1.0 for i in range(n)])
    score = nov * bonus
    thr = np.percentile(score[score > 0], 80) if (score > 0).any() else 1e9
    cuts = []
    for i in np.argsort(-score):
        if score[i] < thr:
            break
        if all(abs(i - c) >= min_len for c in cuts) and min_len <= i <= n - min_len // 2:
            cuts.append(int(i))
    # 拍なし区間の端も区切りにする
    times = np.array([b["time"] for b in bars])
    for s, e in a["no_beat"]:
        for t in (s, e):
            k = int(np.searchsorted(times, t))
            if 0 < k < n and all(abs(k - c) >= 4 for c in cuts):
                cuts.append(k)
    cuts = sorted(set([0] + cuts + [n]))
    # 区切りの強さ（ノベルティ）が弱いものから外し、各区間 12 小節以上・区間数は約 35 秒に 1 つ以下にする
    # （細かすぎると候補として選びにくい。So What で 17 区間になったため）
    max_segs = max(4, int(a["duration"] / 35))
    strength = {c: score[c] if 0 < c < n else np.inf for c in cuts}
    nb_cuts = {int(np.searchsorted(times, t)) for s2, e2 in a["no_beat"] for t in (s2, e2)}
    for c in nb_cuts:                # 拍なし区間の端は残しやすくする
        if c in strength and 0 < c < n:
            strength[c] = max(strength[c], thr)
    while True:
        inner = cuts[1:-1]
        lens = np.diff(cuts)
        # 短さは小節数と秒数の両方で見る（テンポが自由なイントロは 30 秒あっても数小節しかない。So What で確認）
        secs = [(times[cuts[i + 1]] if cuts[i + 1] < n else a["duration"]) - times[cuts[i]] for i in range(len(lens))]
        short = [i for i, L in enumerate(lens) if L < 12 and secs[i] < 20]
        if not inner or (not short and len(cuts) - 1 <= max_segs):
            break
        if short:
            i = short[0]
            # 短い区間は、両隣のうち区切りが弱い方と合体する
            cand = [c for c in (cuts[i], cuts[i + 1]) if 0 < c < n]
        else:
            cand = inner
        weakest = min(cand, key=lambda c: strength[c])
        cuts.remove(weakest)
    segs = []
    q1, q2 = np.percentile(rms, [33, 66])
    for s, e in zip(cuts[:-1], cuts[1:]):
        t0 = times[s]
        t1 = times[e] if e < n else a["duration"]
        r = float(rms[s:e].mean())
        tags = ["静か" if r < q1 else "盛り上がり" if r > q2 else "中くらい"]
        if low[s:e].mean() < np.percentile(low, 25):
            tags.append("低音少なめ")
        if harm[s:e].mean() < np.percentile(harm, 25):
            tags.append("和音うすめ")
        nb = sum(max(0.0, min(t1, e2) - max(t0, s2)) for s2, e2 in a["no_beat"])
        if nb > 0.5 * (t1 - t0):
            tags.append("拍なし")
        segs.append({"bar0": int(s), "bar1": int(e), "t0": round(float(t0), 2), "t1": round(float(t1), 2),
                     "bars": int(e - s), "energy": round(r, 3), "tags": tags})
    if segs:
        segs[0]["tags"].insert(0, "イントロ候補")
        segs[-1]["tags"].insert(0, "アウトロ候補")
    return a, segs


def plot(track_id: int, title: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    matplotlib.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "sans-serif"]

    a, segs = sections(track_id)
    w = np.fromfile(cache.track_dir(track_id) / "wave.u8", dtype=np.uint8).reshape(3, -1)
    t = np.arange(w.shape[1]) / a["wave"]["rate"]
    fig, ax = plt.subplots(figsize=(14, 2.6))
    for band, col, sc in ((0, "#2563eb", 1.0), (1, "#f59e0b", 0.72), (2, "#aaaaaa", 0.42)):
        ax.fill_between(t, -w[band].astype(float) * sc, w[band].astype(float) * sc, color=col, lw=0)
    colors = {"静か": "#16a34a", "中くらい": "#64748b", "盛り上がり": "#dc2626"}
    for sgm in segs:
        ax.axvline(sgm["t0"], color="k", lw=1)
        ax.text(sgm["t0"] + 1, 230, " ".join(x for x in sgm["tags"] if x not in ("イントロ候補", "アウトロ候補")),
                fontsize=7, color=colors.get(sgm["tags"][1] if sgm["tags"][0].endswith("候補") else sgm["tags"][0], "k"),
                va="top")
    ax.set_xlim(0, a["duration"])
    ax.set_ylim(-260, 260)
    ax.set_yticks([])
    ax.set_title(f"{title}  （{len(segs)} 区間）", fontsize=10)
    ax.set_xlabel("秒")
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"sections_{track_id}.png"
    fig.savefig(p, dpi=100)
    plt.close(fig)
    return p


if __name__ == "__main__":
    for tid, title in ((5327, "So What"), (1602, "Asayake"), (1596, "Living On A Feeling"), (2220, "Technologic"), (1327, "Blue Train")):
        a, segs = sections(tid)
        print(title, [(s["t0"], s["bars"], "/".join(s["tags"])) for s in segs])
        plot(tid, title)
