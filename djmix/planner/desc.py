"""おまかせ用の要約（design_phase3.md §3.2・§4.1）。解析キャッシュから作り、track_desc テーブルに保存する。

要約: BPM・拍子・キー・曲全体の特徴量（extra）・自動で決めた使う範囲・その頭と終わりの繋ぐ部分（16 小節）の特徴。
探索のたびに analysis.json（拍の列などで 1 曲数百 KB）を 1 万曲分読まずに済むようにする。
"""
from __future__ import annotations

import json

import numpy as np

from .. import db
from ..analysis import cache, sections
from ..analysis.features import BAR_COLUMNS

DESC_VERSION = 4   # 2: 使う範囲の説明の重複を除いた / 3: 使う範囲の小節ごとのボーカルの量（Phase 4 B1。分離が済んだ曲だけ）
#                    4: 繋ぐ部分を拍の取れる所へ（Phase 5 F。design_phase5.md §F）
STABLE_MOVE_SEC = 90      # 繋ぐ部分を拍の取れる所へずらす上限（秒。使う範囲の 40% までは許す）
STABLE_KEEP_SEC = 120     # ずらしても使う範囲はこれだけ残す
REGION_BARS = 16          # 相性を比べる「繋ぐ部分」の長さ
MAX_USE_SEC = 330         # 使う範囲の上限の目安（長い曲は区間の区切りで切る）
MIN_USE_SEC = 150


def _fmt(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _nobeat(a: dict, t0: float, t1: float) -> float:
    return sum(max(0.0, min(t1, e) - max(t0, s)) for s, e in a["no_beat"])


def choose_use(a: dict, secs: list[dict]) -> dict:
    """使う範囲 {t0, t1, bar0, bar1, notes}。試作 work/phase3/planner_proto.py と同じ規則（design_phase3.md §4.1）。"""
    bars = [b["time"] for b in a["bars"]]
    bt = bars + [a["duration"]]
    start = next((s for s in secs if "拍なし" not in s["tags"]), secs[0])
    end = secs[-1]
    notes = []
    if start is not secs[0]:
        notes.append("拍の取れないイントロを飛ばす")
    for s in reversed(secs):
        if "拍なし" not in s["tags"] and s["t1"] > start["t0"] + 60:
            end = s
            break
    if end is not secs[-1]:
        notes.append("拍の取れない終わりを切る")
    if end["t1"] - start["t0"] > MAX_USE_SEC:
        cand = [s for s in secs if MIN_USE_SEC <= s["t1"] - start["t0"] <= MAX_USE_SEC and "拍なし" not in s["tags"]]
        if cand:
            end = cand[-1]
            notes.append(f"長いので {_fmt(end['t1'])} の区切りで終える")
    b0, b1 = start["bar0"], min(end["bar1"], len(bars))
    # 区間より短い拍の取れない部分（最後のフェードアウト、冒頭の数秒）も繋ぐ部分から外す
    for s0, e0 in a["no_beat"]:
        if bt[b1] - 30 < s0 < bt[b1] and s0 > bt[b0] + 60:
            b1 = max(b0 + 8, max(i for i, x in enumerate(bars) if x <= s0))
            notes.append("最後の拍が取れない部分を切る")
        if bt[b0] <= e0 < bt[b0] + 30 and bt[b1] - e0 > 60:
            b0 = min(b1 - 8, min((i for i, x in enumerate(bars) if x >= e0), default=b0))
            notes.append("最初の拍が取れない部分を飛ばす")
    b1 = min(max(b1, b0 + 1), len(bars))
    b0, b1 = stable_bounds(a, bt, b0, b1, notes)
    return {"t0": round(bt[b0], 3), "t1": round(bt[b1], 3), "bar0": int(b0), "bar1": int(b1), "notes": list(dict.fromkeys(notes))}


def stable_bounds(a: dict, bt: list[float], b0: int, b1: int, notes: list[str] | None = None) -> tuple[int, int]:
    """繋ぐ部分（終わりと始まりの REGION_BARS 小節）に拍の取れない所があれば、拍の取れる所で終わる・始まるよう範囲をずらす（Phase 5 F）。
    ずらすのは最大 max(90 秒, 使う範囲の 40%)、使う範囲は 2 分以上残す。見つからなければそのまま（エコーアウトで繋ぐ）。
    つなぎ目エディタの「拍の取れる所で繋ぐ」（transition.js の stableBounds）と同じ規則。"""
    R = REGION_BARS
    ok = lambda s, e: _nobeat(a, bt[s], bt[e]) < 1
    span = bt[b1] - bt[b0]
    limit = max(STABLE_MOVE_SEC, 0.4 * span)
    if b1 - b0 > R and not ok(b1 - R, b1):
        for e in range(b1 - 1, b0 + R - 1, -1):
            if bt[b1] - bt[e] > limit or bt[e] - bt[b0] < STABLE_KEEP_SEC:
                break
            if ok(e - R, e):
                b1 = e
                if notes is not None:
                    notes.append("拍の取れる所で終える")
                break
    if b1 - b0 > R and not ok(b0, b0 + R):
        for s in range(b0 + 1, b1 - R + 1):
            if bt[s] - bt[b0] > limit or bt[b1] - bt[s] < STABLE_KEEP_SEC:
                break
            if ok(s, s + R):
                b0 = s
                if notes is not None:
                    notes.append("拍の取れる所から始める")
                break
    return b0, b1


def _region(a: dict, feats: np.ndarray, use: dict, side: str) -> dict:
    b0, b1 = ((use["bar0"], min(use["bar0"] + REGION_BARS, use["bar1"])) if side == "in"
              else (max(use["bar0"], use["bar1"] - REGION_BARS), use["bar1"]))
    seg = feats[b0:b1] if b1 > b0 else feats[max(0, b0 - 1):b0 + 1]
    bt = [b["time"] for b in a["bars"]] + [a["duration"]]
    return {"chroma": [round(float(x), 4) for x in seg[:, 2:14].mean(axis=0)],
            "harm": round(float(seg[:, 14].mean()), 4), "low": round(float(seg[:, 1].mean()), 4),
            "nobeat": round(_nobeat(a, bt[b0], bt[min(b1, len(bt) - 1)]), 2)}


def build(a: dict, feats: np.ndarray, stems: np.ndarray | None = None) -> dict | None:
    """要約を作る。小節が少なすぎる・拍が取れない曲は None（おまかせの候補にしない）。"""
    if a.get("bpm", 0) <= 0 or len(a.get("bars", [])) < 16 or len(feats) != len(a["bars"]) or "extra" not in a:
        return None
    secs = sections.compute(a, feats)
    use = choose_use(a, secs)
    return {
        "bpm": a["bpm"], "meter": a["meter"], "duration": a["duration"], "beat_class": a.get("beat_class"),
        "key": a["key"]["camelot"], "key_head": a["key_head30"]["camelot"], "key_tail": a["key_tail30"]["camelot"],
        "extra": a["extra"], "use": use,
        "in": _region(a, feats, use, "in"), "out": _region(a, feats, use, "out"),
        # 使う範囲の小節ごとのボーカルの量（0..1）。音源分離が済んでいない曲は None
        "vocals": ([round(float(v), 2) for v in stems[use["bar0"]:use["bar1"], 0]] if stems is not None and len(stems) == len(a["bars"]) else None),
    }


def update(track_id: int, a: dict, feats: np.ndarray, db_path=None, stems: np.ndarray | None = None) -> dict | None:
    if stems is None:
        from ..analysis import stems as stems_mod
        stems = stems_mod.read(track_id)
    d = build(a, feats, stems)
    with db.tx(db_path) as c:
        if d is None:
            c.execute("DELETE FROM track_desc WHERE track_id = ?", (track_id,))
        else:
            c.execute("INSERT INTO track_desc(track_id, version, data) VALUES(?, ?, ?) ON CONFLICT(track_id) DO UPDATE SET "
                      "version = excluded.version, data = excluded.data", (track_id, DESC_VERSION, json.dumps(d, ensure_ascii=False)))
    return d


def backfill(db_path=None) -> int:
    """解析済みなのに要約が無い・古い曲の要約を、解析キャッシュから作る。作った数を返す。"""
    c = db.connect(db_path)
    ids = [r[0] for r in c.execute(
        "SELECT t.id FROM tracks t LEFT JOIN track_desc d ON d.track_id = t.id "
        "WHERE t.analysis_status = 'done' AND (d.track_id IS NULL OR d.version != ?)", (DESC_VERSION,))]
    n = 0
    for tid in ids:
        a = cache.read(tid)
        feats = cache.bars_array(tid, len(BAR_COLUMNS))
        if a is not None and feats is not None:
            update(tid, a, feats, db_path)
            n += 1
    return n


def load_all(db_path=None) -> dict[int, dict]:
    """要約のある曲すべて（id → 要約＋タグ）。"""
    from ..library.roots import where as roots_where
    rw, rargs = roots_where("t", db_path)
    c = db.connect(db_path)
    out = {}
    for r in c.execute("SELECT d.track_id, d.data, t.title, t.artist, t.album, t.genre, t.path FROM track_desc d "
                       f"JOIN tracks t ON t.id = d.track_id WHERE d.version = ? AND t.analysis_status = 'done' AND {rw}", (DESC_VERSION, *rargs)):
        d = json.loads(r["data"])
        d.update(id=r["track_id"], title=r["title"], artist=r["artist"], album=r["album"], genre=r["genre"])
        out[r["track_id"]] = d
    return out
