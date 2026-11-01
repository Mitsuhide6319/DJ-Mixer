"""拍子と小節頭の位相（design_phase1.md §3.3 の 6、打合せ5）。

Phase 0 で、ジャズ・フュージョンでは検出された小節頭の多数決でも位相が曲中で十数回ずれることが分かった。
そこで各拍の「小節頭らしさ」（ダウンビート確率）を使い、
1. 拍子 M: 小節頭らしさが M 拍ごとに最もはっきり周期的に現れる M（3〜7）。4 を優先し、他はコントラストが明確に上回るときだけ
2. 位相: 8 小節の窓ごとに、M 拍おきの小節頭らしさの合計が最大になる位相
3. 信頼度: 窓の中での 1 位と 2 位の位相の差
を求める。ユーザーの拍子指定・1 拍目指定（user_edits）は API 側で上書きする。
"""
from __future__ import annotations

import numpy as np

from .beats import FPS, gap_mask

METERS = (3, 4, 5, 6, 7)


def downbeat_strength(beats: np.ndarray, down_prob: np.ndarray) -> np.ndarray:
    """各拍の小節頭らしさ（拍の前後 1 フレームの最大値）。"""
    fr = np.clip((beats * FPS).round().astype(int), 0, len(down_prob) - 1)
    lo, hi = np.clip(fr - 1, 0, len(down_prob) - 1), np.clip(fr + 1, 0, len(down_prob) - 1)
    return np.maximum(np.maximum(down_prob[lo], down_prob[fr]), down_prob[hi])


def _contrast(s: np.ndarray, m: int) -> float:
    """位相を最良にしたときの「M 拍おきの平均 − それ以外の平均」。窓ごとに位相を選び、中央値をとる。"""
    win = m * 8
    vals = []
    for st in range(0, max(1, len(s) - win + 1), m * 2):
        w = s[st:st + win]
        if len(w) < m * 4:
            continue
        best = max(w[p::m].mean() - np.delete(w, np.arange(p, len(w), m)).mean() for p in range(m))
        vals.append(best)
    return float(np.median(vals)) if vals else 0.0


def estimate_meter(strength: np.ndarray) -> tuple[int, dict]:
    scores = {m: _contrast(strength, m) for m in METERS}
    best = max(scores, key=scores.get)
    # 4 拍子を基準にし、他の拍子は明確に上回るときだけ採用する（ジャズの 4 拍子が 2 や 8 と紛れやすい）
    if best != 4 and scores[best] < scores[4] * 1.15 + 0.02:
        best = 4
    # 6 は 3 の倍数なので、3 と区別できないときは 3 にそろえる
    if best == 6 and scores[3] >= scores[6] * 0.9:
        best = 3
    return best, {str(k): round(v, 4) for k, v in scores.items()}


def bar_phases(beats: np.ndarray, strength: np.ndarray, m: int) -> list[dict]:
    """小節のリスト [{beat, time, conf}]。拍の途切れ（拍なし区間）の後は位相を推定し直す。"""
    n = len(beats)
    if n < m:
        return []
    seg_id = np.concatenate([[0], np.cumsum(gap_mask(beats))])
    bars = []
    for sid in np.unique(seg_id):
        idx = np.where(seg_id == sid)[0]
        if len(idx) < m:
            continue
        s = strength[idx]
        win = m * 8
        # 各拍に「その拍を中心とする窓での最良位相」と信頼度を割り当てる
        phase = np.zeros(len(idx), dtype=int)
        conf = np.zeros(len(idx))
        step = m
        centers = list(range(0, len(idx), step))
        for c in centers:
            a, b = max(0, c - win // 2), min(len(idx), c + win // 2)
            w = s[a:b]
            sc = np.array([w[(p - a) % m::m].sum() / max(1, len(w[(p - a) % m::m])) for p in range(m)])
            order = np.argsort(sc)[::-1]
            p = int(order[0])
            margin = float((sc[order[0]] - sc[order[1]]) / (sc[order[0]] + 1e-6))
            phase[c:c + step] = p
            conf[c:c + step] = margin
        # 位相が決まった拍のうち「(i - p) % m == 0」を小節頭とする。位相が切り替わる所で近すぎる小節頭は捨てる
        last = -10 ** 9
        for j in range(len(idx)):
            if (j - phase[j]) % m == 0 and j - last >= m - 1:
                bars.append({"beat": int(idx[j]), "time": round(float(beats[idx[j]]), 4),
                             "conf": round(float(np.clip(conf[j] * 2, 0, 1)), 3)})
                last = j
    return bars


# ---------------------------------------------------------------- 拍子が途中で変わる曲（2026-10-25。拍単位の Viterbi）
# 調査: report.md「調査: 拍子が途中で変わる曲の小節頭」、vault の TechResearch/HowTo/2026-10-24_拍単位Viterbiによる拍子変化の追跡_HowTo.md。
# 状態は (拍子 m, 小節内の位置 b)。小節の途中は b → b+1 だけ、小節の終わりで同じ拍子の頭へ（罰則なし）か別の拍子の頭へ（罰則 λ）。
# 観測は、小節頭の状態に log p、それ以外に log(1−p)（p は各拍の小節頭らしさ）。4/4 の小節頭には小さな加点（estimate_meter の「4 を優先」と同じ考え）。
# λ は論文（Fuentes 2018）の 13.8（切り替え確率 10⁻⁶）だと、4/4 に 1 小節だけ挟まる 3/4 を 6 割しか拾えない。合成データで 93〜98% 拾えた 8 から始める。
SWITCH_PENALTY = 8.0
PRIOR4 = 1.5


NEUTRAL_WIN = 32         # 局所の平均の窓（拍）


def local_baseline(strength: np.ndarray, win: int = NEUTRAL_WIN) -> np.ndarray:
    """各拍の前後 win 拍の小節頭らしさの平均（観測を「その辺りの平均と比べてどうか」にするため）。"""
    s = np.asarray(strength, dtype=float)
    k = np.ones(win) / win
    pad = np.pad(s, (win // 2, win - win // 2 - 1), mode="reflect") if len(s) > win else np.full(len(s) + win - 1, s.mean())
    return np.convolve(pad, k, mode="valid")[: len(s)]


def track_meter(strength: np.ndarray, meters=METERS, switch_penalty: float = SWITCH_PENALTY, prior4: float = PRIOR4,
                eps: float = 1e-6, neutral: bool = True) -> list[tuple[int, int]]:
    """各拍の小節頭らしさ strength[K] から、拍子の途中変化を許す Viterbi で、拍ごとの (拍子 m, 小節内の位置 b) を返す（b=1 が小節頭）。"""
    states = [(m, b) for m in meters for b in range(1, m + 1)]
    idx = {s: i for i, s in enumerate(states)}
    n, K = len(states), len(strength)
    if K == 0:
        return []
    trans = np.full((n, n), -np.inf)
    for (m, b), i in idx.items():
        if b < m:
            trans[i, idx[(m, b + 1)]] = 0.0
        else:
            for m2 in meters:
                trans[i, idx[(m2, 1)]] = 0.0 if m2 == m else -switch_penalty
    is_down = np.array([b == 1 for _, b in states])
    bias = np.array([prior4 / 4 if (m == 4 and b == 1) else 0.0 for m, b in states])
    p = np.clip(np.asarray(strength, dtype=float), eps, 1 - eps)
    obs = np.where(is_down[None, :], np.log(p)[:, None], np.log(1 - p)[:, None]) + bias[None, :]
    if neutral:
        # 小節頭らしさが全体に低く平らな所では、log p − log(1−p) < 0 のため「小節頭の少ない長い拍子（6・7）」ほど得になってしまう。
        # 局所の平均 p̄ に対して log((1−p̄)/p̄) を小節頭の状態に足し、平らな所ではどの拍子も同じ点（4 の加点で 4 になる）にする
        pb = np.clip(local_baseline(p), eps, 1 - eps)
        obs = obs + np.where(is_down[None, :], np.log((1 - pb) / pb)[:, None], 0.0)
    score = obs[0] + np.array([prior4 if m == 4 else 0.0 for m, _ in states])   # 先頭はどの位置から始まってもよい
    back = np.zeros((K, n), dtype=np.int16)
    for k in range(1, K):
        cand = score[:, None] + trans
        back[k] = cand.argmax(axis=0)
        score = cand.max(axis=0) + obs[k]
    path = [int(score.argmax())]
    for k in range(K - 1, 0, -1):
        path.append(int(back[k, path[-1]]))
    return [states[i] for i in reversed(path)]


def bar_phases_varying(beats: np.ndarray, strength: np.ndarray, **kw) -> list[dict]:
    """拍子の途中変化を許した小節のリスト [{beat, time, conf, meter}]。拍の途切れ（拍なし区間）ごとに分けて Viterbi を掛ける。
    conf は bar_phases と同じ尺度（小節頭の小節頭らしさと、同じ小節の他の拍との差）。"""
    n = len(beats)
    if n < 3:
        return []
    seg_id = np.concatenate([[0], np.cumsum(gap_mask(beats))])
    bars = []
    for sid in np.unique(seg_id):
        idx = np.where(seg_id == sid)[0]
        if len(idx) < 3:
            continue
        s = strength[idx]
        path = track_meter(s, **kw)
        heads = [j for j, (_, b) in enumerate(path) if b == 1]
        for a, j in enumerate(heads):
            m = path[j][0]
            end = heads[a + 1] if a + 1 < len(heads) else min(len(idx), j + m)
            rest = s[j + 1:end]
            margin = (s[j] - rest.mean()) / (s[j] + 1e-6) if len(rest) else 0.0
            bars.append({"beat": int(idx[j]), "time": round(float(beats[idx[j]]), 4),
                         "conf": round(float(np.clip(margin * 2, 0, 1)), 3), "meter": int(m)})
    return bars


def meter_summary(bars: list[dict]) -> dict:
    """bar_phases_varying の結果のまとめ: 多数決の拍子（既存の meter と互換）・切り替えの回数・拍子ごとの小節数。"""
    ms = [b["meter"] for b in bars]
    if not ms:
        return {"meter": 4, "switches": 0, "counts": {}}
    counts = {int(m): ms.count(m) for m in sorted(set(ms))}
    return {"meter": max(counts, key=counts.get), "switches": sum(1 for a, b in zip(ms, ms[1:]) if a != b), "counts": counts}
