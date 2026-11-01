"""ライブラリの検索・一覧。"""
from __future__ import annotations

from .. import db

# ★・再生回数・最後にかけた日は、アプリの値と iTunes から取り込んだ値を合わせる（Phase 5 E。design_phase5.md §E-3）
RATING_IT = "CASE WHEN COALESCE(p.rating, 0) > 0 THEN p.rating ELSE COALESCE(p.itunes_rating, 0) END"
RATING_APP = "COALESCE(p.rating, 0)"
PLAYS = "(COALESCE(p.play_count, 0) + COALESCE(p.itunes_plays, 0))"
LAST = "NULLIF(MAX(COALESCE(p.last_played, 0), COALESCE(p.itunes_last_played, 0)), 0)"


def rating_expr(db_path=None) -> str:
    from .. import settings
    s = settings.load(db_path)
    return RATING_IT if s.get("itunes_use_rating", True) and s.get("itunes_enabled", True) else RATING_APP


def track_cols(db_path=None) -> str:
    r = rating_expr(db_path)
    return ("t.id, t.title, t.artist, t.album, t.genre, t.year, t.duration, t.folder, t.analysis_status, "
            f"t.bpm, t.camelot, t.lufs, t.beat_class, {r} AS rating, COALESCE(p.rating, 0) AS rating_app, "
            f"{'COALESCE(p.itunes_rating, 0)' if r == RATING_IT else '0'} AS rating_it, "
            f"CASE WHEN COALESCE(p.rating, 0) > 0 THEN 'app' WHEN ({r}) > 0 THEN 'itunes' ELSE NULL END AS rating_src, "
            f"{PLAYS} AS plays, {LAST} AS last_played, "
            "CASE WHEN COALESCE(p.itunes_last_played, 0) > COALESCE(p.last_played, 0) THEN 1 ELSE 0 END AS last_itunes")


FROM = "tracks t LEFT JOIN track_prefs p ON p.track_id = t.id"      # ★ と最後にかけた日（Phase 4）

# 並べ替え（I-1）: 列 → 並べる式。空の値は常に最後。同じ値の中はパス順
SORTS = {
    "title": "t.title COLLATE NOCASE", "artist": "t.artist COLLATE NOCASE", "album": "t.album COLLATE NOCASE",
    "duration": "t.duration", "bpm": "t.bpm", "rating": "RATING", "last_played": LAST, "plays": f"NULLIF({PLAYS}, 0)",
    "camelot": "CAST(substr(t.camelot, 1, length(t.camelot) - 1) AS INTEGER) * 2 + (substr(t.camelot, -1) = 'B')",
    "path": "t.path",
}


def order_by(sort: str, desc: bool = False, db_path=None, playlist: bool = False) -> str:
    if playlist and sort in ("path", "pos"):         # プレイリストの順（プレイリストを選んでいるときの既定）
        return f"pl.pos {'DESC' if desc else 'ASC'}"
    e = SORTS.get(sort, SORTS["path"])
    if e == "RATING":
        e = f"NULLIF({rating_expr(db_path)}, 0)"
    tie = ", t.album COLLATE NOCASE, t.path" if sort == "artist" else ", t.path"
    return f"({e}) IS NULL, {e} {'DESC' if desc else 'ASC'}{tie}"


def track_dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


def search(q: str = "", folder: str | None = None, analyzed: bool | None = None,
           sort: str = "path", offset: int = 0, limit: int = 200, db_path=None, desc: bool = False, root: str | None = None,
           artist: str | None = None, album: str | None = None, playlist: int | None = None,
           artist_q: str = "", album_q: str = "", bpm_min: float | None = None, bpm_max: float | None = None,
           keys: list[str] | None = None, groups: list[str] | None = None, rating_min: int | None = None) -> dict:
    """artist_q / album_q: アーティスト・アルバムに含まれる文字（部分一致）。bpm_min/max: BPM の範囲（倍/半は見ない）。
    keys: Camelot（例 ["8A", "9B"]）。groups: ジャンルのまとまり（planner/genres.py）。rating_min: ★ がこれ以上（アプリ＞iTunes の ★）。"""
    from .roots import where as roots_where
    rw, rargs = roots_where("t", db_path)
    where, args = [rw], list(rargs)            # 設定の音楽フォルダの曲だけ（外したフォルダの曲は隠す。I-3）
    for word in q.split():
        # 空白区切りの各語が、曲名・アーティスト・アルバム・フォルダのどれかに含まれる
        where.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ? OR t.folder LIKE ?)")
        args += [f"%{word}%"] * 4
    if root is not None:
        where.append("t.root = ?")
        args.append(root)
    if folder is not None:
        where.append("t.folder = ?")
        args.append(folder)
    # アーティスト・アルバムで絞り込む（I-4）。"" は「不明（タグ無し）」
    for col, v in (("artist", artist), ("album", album)):
        if v is not None:
            where.append(f"COALESCE(t.{col}, '') = ?")
            args.append(v)
    # 起点の曲を選ぶ小窓などの絞り込み（使ってみての改善）
    for col, v in (("artist", artist_q), ("album", album_q)):
        for word in (v or "").split():
            where.append(f"t.{col} LIKE ?")
            args.append(f"%{word}%")
    if bpm_min is not None:
        where.append("t.bpm >= ?")
        args.append(float(bpm_min))
    if bpm_max is not None:
        where.append("t.bpm <= ?")
        args.append(float(bpm_max))
    if keys:
        where.append(f"t.camelot IN ({','.join('?' * len(keys))})")
        args += list(keys)
    if groups:
        from ..planner.genres import group_of
        c0 = db.connect(db_path)
        gs = set(groups)
        genres = [g for (g,) in c0.execute("SELECT DISTINCT COALESCE(genre, '') FROM tracks") if group_of(g or None) in gs]
        if not genres:
            where.append("0")
        else:
            where.append(f"COALESCE(t.genre, '') IN ({','.join('?' * len(genres))})")
            args += genres
    if rating_min:
        where.append(f"({rating_expr(db_path)}) >= ?")
        args.append(int(rating_min))
    if analyzed is True:
        where.append("t.analysis_status = 'done'")
    elif analyzed is False:
        where.append("t.analysis_status != 'done'")
    frm = FROM
    if playlist is not None:                         # iTunes のプレイリスト（Phase 5 E）
        frm += " JOIN itunes_playlist_items pl ON pl.track_id = t.id AND pl.playlist_id = ?"
        args = [playlist] + args
    w = ("WHERE " + " AND ".join(where)) if where else ""
    c = db.connect(db_path)
    total = c.execute(f"SELECT COUNT(*) FROM {frm} {w}", args).fetchone()[0]
    rows = c.execute(f"SELECT {track_cols(db_path)}{', pl.pos AS pl_pos' if playlist is not None else ''} FROM {frm} {w} "
                     f"ORDER BY {order_by(sort, desc, db_path, playlist is not None)} LIMIT ? OFFSET ?",
                     args + [limit, offset]).fetchall()
    return {"total": total, "offset": offset, "items": [track_dict(r) for r in rows]}


def folders(db_path=None) -> list[dict]:
    """音楽フォルダごとの、直下のフォルダと曲数（設定の音楽フォルダだけ）。"""
    from .roots import where as roots_where
    rw, rargs = roots_where("", db_path)
    c = db.connect(db_path)
    return [{"root": r[0], "folder": r[1], "count": r[2]} for r in
            c.execute(f"SELECT root, folder, COUNT(*) FROM tracks WHERE {rw} GROUP BY root, folder ORDER BY root, folder COLLATE NOCASE", rargs)]


def facets(kind: str, artist: str | None = None, db_path=None) -> list[dict]:
    """アーティスト／アルバムの一覧と曲数（I-4。設定の音楽フォルダだけ）。アルバムは artist で絞れる。
    アルバムには代表のアーティスト（1 人ならその名前、複数なら人数）も付ける。"""
    from .roots import where as roots_where
    rw, rargs = roots_where("", db_path)
    c = db.connect(db_path)
    if kind == "artist":
        return [{"name": r[0], "count": r[1]} for r in c.execute(
            f"SELECT COALESCE(artist, '') a, COUNT(*) FROM tracks WHERE {rw} GROUP BY a ORDER BY a = '', a COLLATE NOCASE", rargs)]
    w, args = rw, list(rargs)
    if artist is not None:
        w += " AND COALESCE(artist, '') = ?"
        args.append(artist)
    return [{"name": r[0], "count": r[1], "artists": r[2], "artist": r[3]} for r in c.execute(
        f"SELECT COALESCE(album, '') b, COUNT(*), COUNT(DISTINCT COALESCE(artist, '')), MIN(COALESCE(artist, '')) "
        f"FROM tracks WHERE {w} GROUP BY b ORDER BY b = '', b COLLATE NOCASE", args)]


def album_list(q: str = "", db_path=None, limit: int = 2000) -> list[dict]:
    """おまかせの「アルバムで選ぶ」（2026-10-25）: アルバム（アルバム名で区別）ごとの曲数・解析済みの曲数・代表のアーティスト。
    q はアルバム名・アーティストの部分一致。設定の音楽フォルダの曲だけ。アルバム名の無い曲は除く。"""
    from .roots import where as roots_where
    rw, rargs = roots_where("", db_path)
    c = db.connect(db_path)
    w, args = rw + " AND COALESCE(album, '') <> ''", list(rargs)
    if q:
        w += " AND (album LIKE ? OR artist LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    rows = c.execute(
        f"SELECT album, COUNT(*), SUM(analysis_status = 'done'), COUNT(DISTINCT COALESCE(artist, '')), MIN(year), "
        f"(SELECT artist FROM tracks t2 WHERE t2.album = tracks.album GROUP BY artist ORDER BY COUNT(*) DESC LIMIT 1) "
        f"FROM tracks WHERE {w} GROUP BY album ORDER BY album COLLATE NOCASE LIMIT ?", (*args, limit)).fetchall()
    return [{"name": r[0], "count": r[1], "analyzed": r[2] or 0, "artists": r[3], "year": r[4], "artist": r[5] or ""} for r in rows]


def album_tracks(albums: list[str], db_path=None) -> list[dict]:
    """指定したアルバムの解析済みの曲（起点の曲を選ぶ一覧に使う）。"""
    if not albums:
        return []
    from .roots import where as roots_where
    rw, rargs = roots_where("t", db_path)
    c = db.connect(db_path)
    marks = ",".join("?" * len(albums))
    return [dict(r) for r in c.execute(
        f"SELECT t.id, t.title, t.artist, t.album, t.bpm, t.camelot, t.duration FROM tracks t "
        f"WHERE t.album IN ({marks}) AND t.analysis_status = 'done' AND {rw} ORDER BY t.album COLLATE NOCASE, t.path", (*albums, *rargs))]


def counts(db_path=None) -> dict:
    from .roots import where as roots_where
    rw, rargs = roots_where("", db_path)
    c = db.connect(db_path)
    r = c.execute(f"SELECT COUNT(*), SUM(analysis_status='done') FROM tracks WHERE {rw}", rargs).fetchone()
    return {"all": r[0], "analyzed": r[1] or 0}


def get_tracks(ids: list[int], db_path=None) -> dict[int, dict]:
    if not ids:
        return {}
    c = db.connect(db_path)
    ph = ",".join("?" * len(ids))
    return {r["id"]: track_dict(r) for r in c.execute(f"SELECT {track_cols(db_path)} FROM {FROM} WHERE t.id IN ({ph})", ids)}
