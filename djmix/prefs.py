"""曲の好み（★）と再生の履歴（Phase 4。design_phase4.md §1.1・§1.4）。"""
from __future__ import annotations

import time

from . import db

SOURCES = ("deck", "mix", "export")


def set_rating(track_id: int, rating: int, db_path=None) -> int:
    rating = max(0, min(5, int(rating)))
    with db.tx(db_path) as c:
        c.execute("INSERT INTO track_prefs(track_id, rating) VALUES(?, ?) ON CONFLICT(track_id) DO UPDATE SET rating = excluded.rating",
                  (track_id, rating))
    return rating


def record_plays(track_ids: list[int], source: str, at: float | None = None, db_path=None) -> int:
    """曲を「かけた」と記録する。同じ曲を同じ source で 10 分以内に記録し直さない（通し再生の繰り返しなどで重複しないように）。"""
    at = at or time.time()
    n = 0
    with db.tx(db_path) as c:
        for tid in dict.fromkeys(track_ids):
            if c.execute("SELECT 1 FROM plays WHERE track_id = ? AND source = ? AND at > ?", (tid, source, at - 600)).fetchone():
                continue
            if c.execute("SELECT 1 FROM tracks WHERE id = ?", (tid,)).fetchone() is None:
                continue
            c.execute("INSERT INTO plays(track_id, at, source) VALUES(?, ?, ?)", (tid, at, source))
            c.execute("INSERT INTO track_prefs(track_id, play_count, last_played) VALUES(?, 1, ?) ON CONFLICT(track_id) DO UPDATE SET "
                      "play_count = play_count + 1, last_played = MAX(COALESCE(last_played, 0), excluded.last_played)", (tid, at))
            n += 1
    return n


def ratings(db_path=None) -> dict[int, int]:
    """曲の ★（アプリの ★ があればそれ、無ければ iTunes の ★。Phase 5 E）。"""
    from .library.query import rating_expr
    return {r[0]: r[1] for r in db.connect(db_path).execute(
        f"SELECT p.track_id, {rating_expr(db_path)} r FROM track_prefs p WHERE r > 0")}


def play_counts(db_path=None) -> dict[int, int]:
    """再生回数（アプリ＋iTunes）。"""
    return {r[0]: r[1] for r in db.connect(db_path).execute(
        "SELECT track_id, COALESCE(play_count, 0) + COALESCE(itunes_plays, 0) n FROM track_prefs WHERE n > 0")}


def recent(days: float, db_path=None) -> set[int]:
    """最近 days 日にかけた曲（アプリの履歴と、iTunes で最後に再生した日）。"""
    since = time.time() - days * 86400
    c = db.connect(db_path)
    out = {r[0] for r in c.execute("SELECT DISTINCT track_id FROM plays WHERE at >= ?", (since,))}
    from . import settings
    if settings.load(db_path).get("itunes_enabled", True):
        out |= {r[0] for r in c.execute("SELECT track_id FROM track_prefs WHERE itunes_last_played >= ?", (since,))}
    return out


def history(limit: int = 50, db_path=None) -> list[dict]:
    c = db.connect(db_path)
    return [{"track_id": r["track_id"], "title": r["title"], "artist": r["artist"], "at": r["at"], "source": r["source"]}
            for r in c.execute("SELECT p.track_id, p.at, p.source, t.title, t.artist FROM plays p JOIN tracks t ON t.id = p.track_id "
                               "ORDER BY p.at DESC LIMIT ?", (limit,))]
