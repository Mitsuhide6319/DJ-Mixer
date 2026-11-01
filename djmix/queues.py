"""ミックスキュー（design_phase1.md §5.3）。Phase 2 のセットリスト・Phase 3 の MixPlan の曲リストと共通のデータ。

作業中のキューは名前 "__current__" で保存し、起動し直しても復元する。「保存」は名前を付けて別に保存する。
"""
from __future__ import annotations

import json
import time

from . import db
from .library.query import get_tracks

CURRENT = "__current__"
ITEM_COLS = ("use_start", "use_end", "use_label", "join_next", "join_bars", "join_tpl", "join_params")


def _queue_id(c, name: str, create: bool = True) -> int | None:
    r = c.execute("SELECT id FROM queues WHERE name = ?", (name,)).fetchone()
    if r:
        return r[0]
    if not create:
        return None
    return c.execute("INSERT INTO queues(name, updated) VALUES(?, ?)", (name, time.time())).lastrowid


def load(name: str = CURRENT, db_path=None) -> list[dict]:
    c = db.connect(db_path)
    qid = _queue_id(c, name, create=False)
    if qid is None:
        return []
    rows = c.execute(f"SELECT track_id, {', '.join(ITEM_COLS)} FROM queue_items WHERE queue_id = ? ORDER BY pos",
                     (qid,)).fetchall()
    tracks = get_tracks([r["track_id"] for r in rows], db_path)
    out = []
    for r in rows:
        if r["track_id"] not in tracks:
            continue
        it = {"track_id": r["track_id"], **{k: r[k] for k in ITEM_COLS}, "track": tracks[r["track_id"]]}
        it["join_params"] = json.loads(it["join_params"]) if it["join_params"] else None
        out.append(it)
    return out


def normalize_joins(items: list[dict]) -> list[dict]:
    """つなぎは「次の曲」とセット。並べ替えで次の曲が変わった（または最後の曲になった）つなぎは標準に戻す。"""
    out = []
    for i, it in enumerate(items):
        it = dict(it)
        nxt = items[i + 1]["track_id"] if i + 1 < len(items) else None
        if it.get("join_next") != nxt or nxt is None:
            it.update(join_next=nxt, join_bars=None, join_tpl=None, join_params=None)
        out.append(it)
    return out


def save(items: list[dict], name: str = CURRENT, db_path=None) -> None:
    items = normalize_joins(items)
    with db.tx(db_path) as c:
        qid = _queue_id(c, name)
        c.execute("DELETE FROM queue_items WHERE queue_id = ?", (qid,))
        c.executemany(
            f"INSERT INTO queue_items(queue_id, pos, track_id, {', '.join(ITEM_COLS)}) VALUES(?,?,?{',?' * len(ITEM_COLS)})",
            [(qid, i, it["track_id"], *[json.dumps(it.get(k)) if k == "join_params" and it.get(k) is not None else it.get(k)
                                          for k in ITEM_COLS])
             for i, it in enumerate(items)])
        c.execute("UPDATE queues SET updated = ? WHERE id = ?", (time.time(), qid))


def names(db_path=None) -> list[dict]:
    c = db.connect(db_path)
    return [{"name": r[0], "updated": r[1], "count": r[2]} for r in c.execute(
        """SELECT q.name, q.updated, COUNT(i.track_id) FROM queues q LEFT JOIN queue_items i ON i.queue_id = q.id
           WHERE q.name != ? GROUP BY q.id ORDER BY q.updated DESC""", (CURRENT,))]
