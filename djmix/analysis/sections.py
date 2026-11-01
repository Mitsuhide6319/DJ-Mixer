"""区間の候補（曲の構造の区切り）。design_phase2.md §2.3、試作は mockup/phase2/sections_proto.py。

解析キャッシュの小節ごとの特徴量（音量・低音・和音の濃さ・クロマ）から、前後 8 小節の平均の差（ノベルティ）が大きい小節を
区切りにする。細かすぎないよう、区切りの弱いものから外して「各区間 12 小節以上（または 20 秒以上）、区間数は約 35 秒に 1 つ以下」
にする。区間には性格の札（静か／中くらい／盛り上がり・低音少なめ・和音うすめ・拍なし・イントロ候補／アウトロ候補）を付ける。
「テーマ」「ソロ」のような音楽的な名前は付けられないので、名前はユーザーが付ける。

結果の時刻は解析時の小節に基づく。ユーザーが拍子を変えても、画面側で今の小節に吸着させて使う。
"""
from __future__ import annotations

import threading

import numpy as np

from . import cache
from .features import BAR_COLUMNS

WIN = 8                 # 前後何小節の平均を比べるか
MIN_BARS, MIN_SEC = 12, 20.0
SEC_PER_SECTION = 35.0

_memo: dict[tuple[int, int], list[dict]] = {}
_lock = threading.Lock()


def _z(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-9)


def compute(a: dict, feats: np.ndarray) -> list[dict]:
    bars = a["bars"]
    n = len(bars)
    if n < 4 or len(feats) != n:
        return [{"bar0": 0, "bar1": n, "t0": 0.0, "t1": round(a["duration"], 2), "bars": n, "tags": ["曲全体"]}]
    times = np.array([b["time"] for b in bars])
    rms, low, harm = feats[:, 0], feats[:, 1], feats[:, -1]
    chroma = feats[:, 2:14]
    chroma = chroma / (np.linalg.norm(chroma, axis=1, keepdims=True) + 1e-9)
    X = np.column_stack([_z(rms) * 1.5, _z(low) * 1.2, _z(harm), chroma])

    nov = np.zeros(n)
    for i in range(WIN, n - WIN + 1):
        nov[i] = np.linalg.norm(X[i - WIN:i].mean(0) - X[i:i + WIN].mean(0))
    score = nov * np.array([1.08 if i % 4 == 0 else 1.0 for i in range(n)])   # 4 小節の頭（フレーズの頭）を少し優遇
    thr = np.percentile(score[score > 0], 80) if (score > 0).any() else np.inf

    cuts: list[int] = []
    for i in np.argsort(-score):
        if score[i] < thr:
            break
        if all(abs(i - c) >= WIN for c in cuts) and WIN <= i <= n - WIN // 2:
            cuts.append(int(i))
    nb_cuts = set()
    for s, e in a["no_beat"]:                                # 拍なし区間の端も区切りにする
        for t in (s, e):
            k = int(np.searchsorted(times, t))
            if 0 < k < n:
                nb_cuts.add(k)
                if all(abs(k - c) >= 4 for c in cuts):
                    cuts.append(k)
    cuts = sorted(set([0] + cuts + [n]))

    # 弱い区切りから外す
    max_segs = max(4, int(a["duration"] / SEC_PER_SECTION))
    strength = {c: (score[c] if 0 < c < n else np.inf) for c in cuts}
    for c in nb_cuts:
        if c in strength:
            strength[c] = max(strength[c], thr)
    end_t = lambda j: times[j] if j < n else a["duration"]   # noqa: E731
    while True:
        lens = np.diff(cuts)
        secs = [end_t(cuts[i + 1]) - times[cuts[i]] for i in range(len(lens))]
        # 短さは小節数と秒数の両方で見る（テンポが自由なイントロは 30 秒あっても数小節しかない）
        short = [i for i, L in enumerate(lens) if L < MIN_BARS and secs[i] < MIN_SEC]
        if len(cuts) <= 2 or (not short and len(cuts) - 1 <= max_segs):
            break
        cand = [c for c in (cuts[short[0]], cuts[short[0] + 1]) if 0 < c < n] if short else cuts[1:-1]
        cuts.remove(min(cand, key=lambda c: strength[c]))

    q1, q2 = np.percentile(rms, [33, 66])
    low25, harm25 = np.percentile(low, 25), np.percentile(harm, 25)
    segs = []
    for s, e in zip(cuts[:-1], cuts[1:]):
        t0, t1 = float(times[s]), float(end_t(e))
        r = float(rms[s:e].mean())
        tags = ["静か" if r < q1 else "盛り上がり" if r > q2 else "中くらい"]
        if low[s:e].mean() < low25:
            tags.append("低音少なめ")
        if harm[s:e].mean() < harm25:
            tags.append("和音うすめ")
        nb = sum(max(0.0, min(t1, e2) - max(t0, s2)) for s2, e2 in a["no_beat"])
        if nb > 0.5 * (t1 - t0):
            tags.append("拍なし")
        segs.append({"bar0": int(s), "bar1": int(e), "t0": round(t0, 3), "t1": round(t1, 3), "bars": int(e - s), "tags": tags})
    if len(segs) > 1:
        segs[0]["tags"].insert(0, "イントロ候補")
        segs[-1]["tags"].insert(0, "アウトロ候補")
    return segs


def for_track(track_id: int) -> list[dict] | None:
    """解析済みなら区間の候補を返す（同じ解析結果には計算結果を使い回す）。"""
    a = cache.read(track_id)
    if a is None:
        return None
    key = (track_id, cache.ANALYSIS_VERSION)
    with _lock:
        if key in _memo:
            return _memo[key]
    feats = cache.bars_array(track_id, len(BAR_COLUMNS))
    segs = compute(a, feats if feats is not None else np.zeros((0, len(BAR_COLUMNS))))
    with _lock:
        _memo[key] = segs
        if len(_memo) > 256:
            _memo.pop(next(iter(_memo)))
    return segs


def forget(track_id: int) -> None:
    """再解析したら呼ぶ。"""
    with _lock:
        for k in [k for k in _memo if k[0] == track_id]:
            del _memo[k]
