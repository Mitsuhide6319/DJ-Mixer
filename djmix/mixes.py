"""ミックス = 名前付きの保存したキュー（Phase 4。design_phase4.md §1）。作業中のキュー（__current__）は含めない。"""
from __future__ import annotations

import json
import time

from . import db, queues

TPL_BARS = {"long": 16, "short": 8, "bass": 16, "filter": 16, "echo": 2, "cut": 0}


def _minutes(items: list[dict]) -> float:
    sec = 0.0
    for i, it in enumerate(items):
        t = it.get("track") or {}
        t0 = it.get("use_start") or 0.0
        t1 = it.get("use_end") if it.get("use_end") is not None else (t.get("duration") or 0.0)
        sec += max(0.0, t1 - t0)
        if i + 1 < len(items):
            bars = it.get("join_bars") if it.get("join_bars") is not None else TPL_BARS.get(it.get("join_tpl") or "long", 16)
            sec -= bars * 4 * 60 / (t.get("bpm") or 120)
    return round(max(0.0, sec) / 60, 1)


def _row(c, r) -> dict:
    return {"id": r["id"], "name": r["name"], "kind": r["kind"] or "hand", "created": r["created"] or r["updated"], "updated": r["updated"],
            "plays": r["plays"] or 0, "last_played": r["last_played"], "exports": json.loads(r["exports"]) if r["exports"] else []}


def unique_name(name: str, db_path=None) -> str:
    c = db.connect(db_path)
    base, n, cand = name.strip() or "ミックス", 2, name.strip() or "ミックス"
    while cand == queues.CURRENT or c.execute("SELECT 1 FROM queues WHERE name = ?", (cand,)).fetchone():
        cand, n = f"{base} ({n})", n + 1
    return cand


def list_mixes(sort: str = "new", db_path=None) -> list[dict]:
    c = db.connect(db_path)
    order = {"new": "COALESCE(created, updated) DESC", "name": "name COLLATE NOCASE", "plays": "plays DESC, updated DESC"}.get(sort, "updated DESC")
    out = []
    for r in c.execute(f"SELECT * FROM queues WHERE name != ? ORDER BY {order}", (queues.CURRENT,)).fetchall():
        m = _row(c, r)
        items = queues.load(r["name"], db_path)
        m.update(count=len(items), minutes=_minutes(items), titles=[it["track"]["title"] for it in items[:4]])
        out.append(m)
    return out


def get(mix_id: int, db_path=None) -> dict | None:
    c = db.connect(db_path)
    r = c.execute("SELECT * FROM queues WHERE id = ? AND name != ?", (mix_id, queues.CURRENT)).fetchone()
    if r is None:
        return None
    items = queues.load(r["name"], db_path)
    return {**_row(c, r), "items": items, "count": len(items), "minutes": _minutes(items)}


def save(items: list[dict], name: str, kind: str = "hand", db_path=None) -> dict:
    """キューの中身を新しいミックスとして残す（同じ名前があれば (2) を付ける）。"""
    name = unique_name(name, db_path)
    queues.save(items, name, db_path)
    now = time.time()
    with db.tx(db_path) as c:
        c.execute("UPDATE queues SET kind = ?, created = ? WHERE name = ?", ("auto" if kind == "auto" else "hand", now, name))
        mid = c.execute("SELECT id FROM queues WHERE name = ?", (name,)).fetchone()[0]
    return get(mid, db_path)


def update_items(mix_id: int, items: list[dict], db_path=None) -> dict | None:
    m = get(mix_id, db_path)
    if m:
        queues.save(items, m["name"], db_path)
    return get(mix_id, db_path)


def rename(mix_id: int, name: str, db_path=None) -> dict | None:
    m = get(mix_id, db_path)
    if not m:
        return None
    if name.strip() and name.strip() != m["name"]:
        with db.tx(db_path) as c:
            c.execute("UPDATE queues SET name = ? WHERE id = ?", (unique_name(name, db_path), mix_id))
    return get(mix_id, db_path)


def duplicate(mix_id: int, db_path=None) -> dict | None:
    m = get(mix_id, db_path)
    return save([{k: v for k, v in it.items()} for it in m["items"]], f"{m['name']} のコピー", m["kind"], db_path) if m else None


def delete(mix_id: int, db_path=None) -> bool:
    with db.tx(db_path) as c:
        return c.execute("DELETE FROM queues WHERE id = ? AND name != ?", (mix_id, queues.CURRENT)).rowcount > 0


def record_play(mix_id: int, db_path=None) -> None:
    with db.tx(db_path) as c:
        c.execute("UPDATE queues SET plays = plays + 1, last_played = ? WHERE id = ?", (time.time(), mix_id))


def add_export(mix_id: int, info: dict, db_path=None) -> None:
    m = get(mix_id, db_path)
    if not m:
        return
    ex = m["exports"] + [{**info, "at": time.time()}]
    with db.tx(db_path) as c:
        c.execute("UPDATE queues SET exports = ? WHERE id = ?", (json.dumps(ex[-20:], ensure_ascii=False), mix_id))


def _signature(items: list[dict]) -> list:
    return [(it["track_id"], round(it.get("use_start") or 0, 1), round(it["use_end"], 1) if it.get("use_end") is not None else None,
             it.get("join_tpl"), it.get("join_bars")) for it in items]


def record_export(info: dict, current_mix: int | None, db_path=None) -> dict:
    """書き出しの後: キューが保存したミックスのままならそのミックスに記録し、変えていれば（または保存していなければ）
    キューの中身を書き出しの名前で新しいミックスとして残す。書き出した曲を「かけた」と記録する。"""
    from . import prefs
    items = queues.load(queues.CURRENT, db_path)
    m = get(current_mix, db_path) if current_mix else None
    if m is None or _signature(m["items"]) != _signature(items):
        from pathlib import Path
        m = save(items, Path(info.get("path", "ミックス")).stem, "hand", db_path)
    add_export(m["id"], {k: info.get(k) for k in ("path", "format", "seconds", "tracklist")}, db_path)
    prefs.record_plays([it["track_id"] for it in items], "export", db_path=db_path)
    return get(m["id"], db_path)


def is_current(mix_id: int | None, db_path=None) -> bool:
    """キューが、そのミックスを開いたままの中身か。"""
    m = get(mix_id, db_path) if mix_id else None
    return bool(m) and _signature(m["items"]) == _signature(queues.load(queues.CURRENT, db_path))
