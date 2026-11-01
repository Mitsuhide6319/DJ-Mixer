"""Phase 3 モックアップ用のデータ（work/phase3/mock3_data.json）。試作（work/phase3/planner_proto.py）の実際の結果。"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "work" / "phase3"))
import os  # noqa: E402
os.chdir(ROOT)
import planner_proto as P  # noqa: E402

GROUPS = [  # ジャンルのまとまり（タグの表記の揺れをまとめる）
    ("ジャズ・フュージョン", r"jazz|fusion|bossa|swing|bebop"),
    ("R&B・ソウル・ファンク", r"r&b|soul|funk"),
    ("J-POP・歌謡", r"j-?pop|domestic|歌謡|kayo|japanese"),
    ("ポップス", r"pop|adult contemporary|easy listening"),
    ("ロック", r"rock|metal|punk|alternative|blues"),
    ("ダンス・エレクトロ", r"dance|house|techno|electro|trance|club"),
    ("サントラ・ゲーム・アニメ", r"soundtrack|game|anime|ost"),
    ("クラシック", r"classical|orchestra"),
]


def group_of(g):
    if not g:
        return "その他"
    for name, rx in GROUPS:
        if re.search(rx, g, re.I):
            return name
    return "その他"


def fix_text(v):
    """古い形式のタグの Shift-JIS が Latin-1 として読まれた文字化けを直す（ライブラリ側の修正は Phase 3 の作業）。"""
    if v and re.search(r"[-ÿ]", v) and not re.search(r"[　-鿿]", v):
        try:
            return v.encode("latin-1").decode("cp932")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return v
    return v


def parts(a, b):
    """相性の内訳（planner_proto.pair と同じ式）。"""
    ts, rate, k = P.tempo_rel(a, b)
    ks, kname = P.camelot_rel(P.key_of(a, "out")["camelot"], P.key_of(b, "in")["camelot"])
    ro, ri = a["_out"], b["_in"]
    hw = min(1.0, 3 * min(ro["harm"], ri["harm"]))
    cos = float(ro["chroma"] @ ri["chroma"] / (np.linalg.norm(ro["chroma"]) * np.linalg.norm(ri["chroma"]) + 1e-9))
    sim = max(0.0, 1 - float(np.linalg.norm(a["timbre"] - b["timbre"])) / 2.5)
    return {"tempo": round(ts, 2), "key": round(1 - hw * (1 - ks), 2), "chroma": round(cos, 2), "mood": round(sim, 2),
            "rate": round(rate, 4), "k": k}


def main():
    tracks = P.load_pool()
    for t in tracks:
        t["_out"], t["_in"] = P.region(t, "out"), P.region(t, "in")
    seed = next(t for t in tracks if t["track"]["title"] == "Asayake")
    jazz = re.compile(GROUPS[0][1], re.I)
    cands = [t for t in tracks if t is seed or (t["track"]["genre"] and jazz.search(t["track"]["genre"]) and P.tempo_rel(seed, t)[0] > 0)]

    def pack_plan(score, seq, length, notes):
        rows = []
        for j, t in enumerate(seq):
            r = {"id": t["track"]["id"]}
            if j + 1 < len(seq):
                s, why, tpl, rate = P.pair(t, seq[j + 1])
                r["join"] = {"tpl": tpl, "score": round(s, 3), "why": why, **parts(t, seq[j + 1])}
            rows.append(r)
        return {"score": round(sum(P.pair(a, b)[0] for a, b in zip(seq, seq[1:])) / (len(seq) - 1), 3),
                "minutes": round(length / 60, 1), "tracks": rows}

    plans = {}
    for flow in ("up", "keep", "peak", "down"):
        plans[flow] = [pack_plan(*p) for p in P.plan(cands, seed, 40, flow)]
        print(flow, [p["score"] for p in plans[flow]])

    # キューの並べ替え（ユーザーのキューは読むだけ）
    c = sqlite3.connect("file:data/library.sqlite?mode=ro", uri=True)
    qid = c.execute("select id from queues order by updated desc limit 1").fetchone()[0]
    ids = [r[0] for r in c.execute("select track_id from queue_items where queue_id=? order by pos", (qid,))]
    byid = {t["track"]["id"]: t for t in tracks}
    q = [byid[i] for i in ids if i in byid]
    best = None
    for s0 in q:
        for p in P.plan(tracks, s0, 999, "keep", fixed_set=q)[:1]:
            pp = pack_plan(*p)
            if not best or pp["score"] > best["score"]:
                best = pp
    orig = pack_plan(0, q, sum(t["use"]["t1"] - t["use"]["t0"] - 30 for t in q) + 30, [])
    plans["queue"] = [best, orig]
    print("queue", orig["score"], "->", best["score"])

    used = {r["id"] for ps in plans.values() for p in ps for r in p["tracks"]}
    # 次の曲の候補（案Y）: 候補群の各曲について上位 8 曲
    nexts = {}
    for a in cands:
        sc = sorted(((P.pair(a, b)[0], b) for b in cands if b is not a), key=lambda x: -x[0])[:8]
        nexts[a["track"]["id"]] = [{"id": b["track"]["id"], "score": round(s, 3), "why": P.pair(a, b)[1], "tpl": P.pair(a, b)[2], **parts(a, b)} for s, b in sc]
        used |= {b["track"]["id"] for _, b in sc}

    out_tracks = {}
    for t in tracks:
        if t["track"]["id"] not in used:
            continue
        f = t["feats"]
        out_tracks[t["track"]["id"]] = {
            "id": t["track"]["id"], "title": fix_text(t["track"]["title"]), "artist": fix_text(t["track"]["artist"]), "genre": t["track"]["genre"],
            "bpm": t["bpm"], "camelot": t["key"]["camelot"], "energy": round(t["energy"], 3), "duration": t["duration"],
            "use": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in t["use"].items()},
            "bars": [round(b["time"], 2) for b in t["bars"]],
            "rms": [round(float(x), 3) for x in f[:, 0]], "low": [round(float(x), 3) for x in f[:, 1]],
        }

    # ジャンルのまとまりごとの曲数（ライブラリ全体と解析済み＝候補群）
    counts = {}
    for (g,) in c.execute("select genre from tracks"):
        counts.setdefault(group_of(g), [0, 0])[0] += 1
    for t in tracks:
        counts.setdefault(group_of(t["track"]["genre"]), [0, 0])[1] += 1
    groups = [{"name": n, "total": counts.get(n, [0, 0])[0], "analyzed": counts.get(n, [0, 0])[1]} for n, _ in GROUPS + [("その他", "")]]
    lib = c.execute("select count(*), sum(duration) from tracks").fetchone()

    data = {"seed": seed["track"]["id"], "plans": plans, "nexts": nexts, "tracks": out_tracks, "groups": groups,
            "library": {"tracks": lib[0], "hours": round(lib[1] / 3600), "analyzed": len(tracks)}, "queue": [t["track"]["id"] for t in q]}
    p = ROOT / "work" / "phase3" / "mock3_data.json"
    p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(p, p.stat().st_size // 1024, "KB", len(out_tracks), "tracks")


if __name__ == "__main__":
    main()
