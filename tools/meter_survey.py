"""拍子が途中で変わる曲の Viterbi（bars.bar_phases_varying）を、解析済みの全曲に掛けて誤検出を数える（読むだけ。何も書き換えない）。

使い方:
  .venv\\Scripts\\python tools\\meter_survey.py [--limit N] [--penalty 8] [--out 結果.csv] [--find 曲名 ...]
集計: 拍子の切り替えが起きた曲の割合（ジャンルのまとまり別）、多数決の拍子と今の拍子（estimate_meter）の一致、
  切り替えの多い曲の一覧。--find で曲名の一部を指定すると、その曲の小節ごとの拍子の並びを出す。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from djmix.analysis import bars, cache  # noqa: E402
from djmix.config import DATA_DIR  # noqa: E402
from djmix.planner.genres import group_of  # noqa: E402


def _groups(ms):
    from itertools import groupby
    k = 0
    for m, g in groupby(ms):
        L = len(list(g))
        yield k, [m] * L
        k += L


def runs(ms: list[int]) -> str:
    """[7,7,7,4,4] → '7×3 4×2'。"""
    out, k = [], 0
    while k < len(ms):
        j = k
        while j < len(ms) and ms[j] == ms[k]:
            j += 1
        out.append(f"{ms[k]}×{j - k}")
        k = j
    return " ".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--penalty", type=float, default=bars.SWITCH_PENALTY)
    ap.add_argument("--out", default="")
    ap.add_argument("--raw", action="store_true", help="局所の平均で正規化しない（調査の元のモデル）")
    ap.add_argument("--find", nargs="*", default=[])
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{DATA_DIR / 'library.sqlite'}?mode=ro", uri=True)    # 読むだけ
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, title, artist, genre FROM tracks WHERE analysis_status = 'done' ORDER BY id").fetchall()
    if a.find:
        keys = [k.casefold() for k in a.find]
        rows = [r for r in rows if any(k in (r["title"] or "").casefold() for k in keys)]
    elif a.limit:
        rows = rows[: a.limit]

    t0, res = time.time(), []
    for r in rows:
        an = cache.read(r["id"])
        dp = cache.downbeat_prob(r["id"])
        if an is None or dp is None or len(an["beats"]) < 8:
            continue
        b = np.asarray(an["beats"], dtype=float)
        s = bars.downbeat_strength(b, dp)
        bl = bars.bar_phases_varying(b, s, switch_penalty=a.penalty, neutral=not a.raw)
        sm = bars.meter_summary(bl)
        ms = [x["meter"] for x in bl]
        minority = 1 - max(sm["counts"].values()) / len(ms) if ms else 0.0
        # 切り替えを「位相の補正」（多数派と違う拍子が 1〜2 小節だけ）と「続く拍子の変化」（4 小節以上）に分ける
        rl = [(ms[k], len(list(g))) for k, g in _groups(ms)]
        dom = sm["meter"]
        fix = sum(1 for m, L in rl if m != dom and L <= 2)
        sus = sum(1 for m, L in rl if m != dom and L >= 4)
        # 今の方式（曲全体で 1 つの拍子＋窓ごとの位相）で、小節の長さ（拍数）が拍子と違う所 = 位相の補正の数
        ob = [x["beat"] for x in an["bars"]]
        old_fix = sum(1 for p, q in zip(ob, ob[1:]) if q - p != an["meter"])
        # 時刻で見た良さ（拍の列に余分な拍が混ざると拍数の拍子は狂うので、小節頭の位置と小節の長さ（秒）で比べる）
        nb = [x["beat"] for x in bl]
        strong = s >= 0.5
        def quality(hs):
            hs = np.asarray(hs, dtype=int)
            if not len(hs):
                return 0.0, 0.0, 0.0
            return float(s[hs].mean()), float(strong[hs].sum() / max(1, strong.sum())), float((s[hs] < 0.2).mean())
        q_new, q_old = quality(nb), quality(ob)
        dur = np.diff([x["time"] for x in bl])
        med = np.median(dur) if len(dur) else 1.0
        odd = (dur < 0.8 * med) | (dur > 1.25 * med)
        tsus, k = 0, 0
        while k < len(odd):
            j = k
            while j < len(odd) and odd[j] == odd[k]:
                j += 1
            if odd[k] and j - k >= 4:
                tsus += 1
            k = j
        res.append({"id": r["id"], "title": r["title"], "artist": r["artist"], "group": group_of(r["genre"]),
                    "old_meter": an["meter"], "new_meter": sm["meter"], "switches": sm["switches"],
                    "bars": len(ms), "minority": round(minority, 3), "fix": fix, "sustained": sus, "old_fix": old_fix, "time_sustained": tsus,
                    "head_s": round(q_new[0], 3), "old_head_s": round(q_old[0], 3), "recall": round(q_new[1], 3), "old_recall": round(q_old[1], 3),
                    "weak": round(q_new[2], 3), "old_weak": round(q_old[2], 3), "runs": runs(ms)})
        if a.find:
            print(f"{r['title']} / {r['artist']}  今の拍子 {an['meter']} → 多数決 {sm['meter']}、切り替え {sm['switches']} 回\n  {runs(ms)}")
    sec = time.time() - t0
    if a.find:
        return

    n = len(res)
    print(f"{n} 曲（{sec:.0f} 秒、1 曲 {sec / max(1, n) * 1000:.1f}ms）、罰則 λ={a.penalty}、{'正規化なし' if a.raw else '局所の平均で正規化'}")
    sw = [x for x in res if x["switches"]]
    print(f"拍子の切り替えがある曲: {len(sw)} 曲（{len(sw) / max(1, n) * 100:.1f}%）")
    print(f"  少数派の拍子の小節が 5% 以上: {sum(x['minority'] >= 0.05 for x in res)} 曲、20% 以上: {sum(x['minority'] >= 0.2 for x in res)} 曲")
    same = sum(x["old_meter"] == x["new_meter"] for x in res)
    print(f"多数決の拍子が今の拍子と一致: {same} 曲（{same / max(1, n) * 100:.1f}%）")
    print("今の拍子 → 多数決の拍子（一致しないもの）:", dict(Counter((x["old_meter"], x["new_meter"]) for x in res if x["old_meter"] != x["new_meter"]).most_common(8)))
    print(f"位相の補正（1〜2 小節だけ違う拍子）: 平均 {np.mean([x['fix'] for x in res]):.1f} 回／曲 ・ 今の方式の位相の補正: 平均 {np.mean([x['old_fix'] for x in res]):.1f} 回／曲")
    mean = lambda k: np.mean([x[k] for x in res])  # noqa: E731
    print(f"小節頭の小節頭らしさの平均: 新 {mean('head_s'):.3f} ／ 今 {mean('old_head_s'):.3f}")
    print(f"強い小節頭（0.5 以上）のうち小節頭にしたもの: 新 {mean('recall') * 100:.1f}% ／ 今 {mean('old_recall') * 100:.1f}%")
    print(f"弱い所（0.2 未満）を小節頭にした割合: 新 {mean('weak') * 100:.1f}% ／ 今 {mean('old_weak') * 100:.1f}%")
    better = sum(x["head_s"] > x["old_head_s"] + 0.01 for x in res); worse = sum(x["head_s"] < x["old_head_s"] - 0.01 for x in res)
    print(f"曲ごと: 新が良い {better} 曲 ・ 今が良い {worse} 曲 ・ ほぼ同じ {n - better - worse} 曲")
    ts = [x for x in res if x["time_sustained"]]
    print(f"小節の長さ（秒）が 4 小節以上続けて変わる曲（本当の拍子の変化の候補）: {len(ts)} 曲（{len(ts) / max(1, n) * 100:.1f}%）")
    su = [x for x in res if x["sustained"]]
    print(f"続く拍子の変化（4 小節以上）がある曲: {len(su)} 曲（{len(su) / max(1, n) * 100:.1f}%）")
    print("切り替えの回数の分布:", dict(sorted(Counter(min(x["switches"], 10) for x in res).items())))
    g = defaultdict(list)
    for x in res:
        g[x["group"]].append(x)
    print("\nジャンルのまとまり別（曲数・切り替えのある曲の割合・少数派 5% 以上の割合）:")
    for k, xs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k}: {len(xs)} 曲、{sum(1 for x in xs if x['switches']) / len(xs) * 100:.1f}%、{sum(1 for x in xs if x['minority'] >= 0.05) / len(xs) * 100:.1f}%")
    print("\n切り替えの多い曲（上位 15）:")
    for x in sorted(res, key=lambda x: -x["switches"])[:15]:
        print(f"  {x['switches']:3d} 回  {x['title']} / {x['artist']}（{x['group']}）  {x['runs'][:90]}")
    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(res[0].keys()))
            w.writeheader()
            w.writerows(res)
        print(f"\n一覧: {a.out}")


if __name__ == "__main__":
    main()
