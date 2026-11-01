"""iTunes の情報の取り込み（Phase 5 E。design_phase5.md §E）。

iTunes の「ライブラリ XML」を**読むだけ**で、★・再生回数・最後に再生した日・プレイリストを取り込む。
iTunes 側の情報も、音楽ファイルのタグも変えない。曲はファイルのパスで対応づける。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import plistlib
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import db, settings

log = logging.getLogger("djmix.itunes")

CANDIDATES = ["iTunes Music Library.xml", "iTunes Library.xml"]
_lock = threading.Lock()


def default_dir() -> Path:
    return Path.home() / "Music" / "iTunes"


def xml_path(db_path=None) -> Path | None:
    """設定の XML、無ければ既定の場所で見つかったもの。"""
    p = (settings.load(db_path).get("itunes_xml") or "").strip()
    if p:
        return Path(p)
    for n in CANDIDATES:
        c = default_dir() / n
        if c.exists():
            return c
    return None


def location_to_path(loc: str) -> str | None:
    """file://localhost/C:/Music/a%20b.mp3 → C:\\Music\\a b.mp3"""
    if not loc or not loc.startswith("file:"):
        return None
    u = urlparse(loc)
    p = unquote(u.path)
    if u.netloc and u.netloc != "localhost":          # ネットワークの共有フォルダ //server/share/...
        p = f"//{u.netloc}{p}"
    elif len(p) >= 3 and p[0] == "/" and p[2] == ":":  # /C:/... → C:/...
        p = p[1:]
    return os.path.normpath(p)


def _key(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _ts(v) -> float | None:
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:                            # plistlib は UTC の naive datetime を返す
            v = v.replace(tzinfo=_dt.timezone.utc)
        return v.timestamp()
    return None


def parse(path: Path) -> dict:
    """XML を読んで {tracks: {iTunes の Track ID: {path, rating, plays, last}}, playlists: [...]} にする。"""
    with open(path, "rb") as f:
        pl = plistlib.load(f)
    tracks = {}
    for tid, t in (pl.get("Tracks") or {}).items():
        p = location_to_path(t.get("Location", ""))
        if not p:
            continue
        rating = 0 if t.get("Rating Computed") else int(t.get("Rating") or 0) // 20
        tracks[int(t.get("Track ID", tid))] = {"path": p, "rating": max(0, min(5, rating)),
                                               "plays": int(t.get("Play Count") or 0), "last": _ts(t.get("Play Date UTC"))}
    playlists = []
    for i, p in enumerate(pl.get("Playlists") or []):
        if p.get("Master") or p.get("Distinguished Kind") is not None or p.get("Visible") is False:
            continue
        playlists.append({"pid": p.get("Playlist Persistent ID") or f"#{i}", "name": p.get("Name") or "（名前なし）",
                          "parent": p.get("Parent Persistent ID"), "folder": bool(p.get("Folder")),
                          "smart": "Smart Info" in p and not p.get("Folder"), "sort": i,
                          "items": [int(x["Track ID"]) for x in p.get("Playlist Items") or [] if "Track ID" in x]})
    return {"tracks": tracks, "playlists": playlists}


def import_xml(path: Path | None = None, db_path=None) -> dict:
    """読み込んで DB に入れる。戻り値（と設定 itunes_status）: 件数・時刻・エラー。"""
    with _lock:
        path = path or xml_path(db_path)
        st = {"path": str(path) if path else None, "at": time.time(), "error": None}
        if path is None or not path.exists():
            st["error"] = "not_found"
            settings.save({"itunes_status": st}, db_path)
            return st
        t0 = time.time()
        try:
            data = parse(path)
        except Exception as e:                       # 書き出し途中などで読めない
            log.warning("itunes xml read failed: %s", e)
            st["error"] = f"XML を読めませんでした: {e}"[:300]
            settings.save({"itunes_status": st}, db_path)
            return st
        c = db.connect(db_path)
        by_path = {_key(r[1]): r[0] for r in c.execute("SELECT id, path FROM tracks")}
        idmap = {}                                    # iTunes の Track ID → アプリの曲
        for itid, t in data["tracks"].items():
            aid = by_path.get(_key(t["path"]))
            if aid is not None:
                idmap[itid] = aid
        with db.tx(db_path) as tx:
            tx.execute("UPDATE track_prefs SET itunes_rating = NULL, itunes_plays = NULL, itunes_last_played = NULL")
            rows = [(aid, data["tracks"][itid]["rating"] or None, data["tracks"][itid]["plays"] or None, data["tracks"][itid]["last"])
                    for itid, aid in idmap.items()]
            rows = [r for r in rows if r[1] or r[2] or r[3]]
            tx.executemany("INSERT INTO track_prefs(track_id, itunes_rating, itunes_plays, itunes_last_played) VALUES(?, ?, ?, ?) "
                           "ON CONFLICT(track_id) DO UPDATE SET itunes_rating = excluded.itunes_rating, "
                           "itunes_plays = excluded.itunes_plays, itunes_last_played = excluded.itunes_last_played", rows)
            tx.execute("DELETE FROM itunes_playlist_items")
            tx.execute("DELETE FROM itunes_playlists")
            for p in data["playlists"]:
                cur = tx.execute("INSERT INTO itunes_playlists(pid, name, parent_pid, is_folder, is_smart, sort) VALUES(?, ?, ?, ?, ?, ?)",
                                 (p["pid"], p["name"], p["parent"], int(p["folder"]), int(p["smart"]), p["sort"]))
                if not p["folder"]:
                    items = [(cur.lastrowid, pos, idmap[x]) for pos, x in enumerate(p["items"]) if x in idmap]
                    tx.executemany("INSERT INTO itunes_playlist_items(playlist_id, pos, track_id) VALUES(?, ?, ?)", items)
        st.update(mtime=path.stat().st_mtime, tracks=len(data["tracks"]), matched=len(idmap),
                  rated=sum(1 for r in rows if r[1]), played=sum(1 for r in rows if r[2]),
                  playlists=sum(1 for p in data["playlists"] if not p["folder"]), sec=round(time.time() - t0, 2))
        settings.save({"itunes_status": st}, db_path)
        log.info("itunes imported: %s", {k: v for k, v in st.items() if k != "path"})
        return st


def unlink(db_path=None) -> None:
    """連携をやめる: iTunes から取り込んだ値とプレイリストを消す（アプリの ★・履歴はそのまま）。"""
    with _lock, db.tx(db_path) as tx:
        tx.execute("UPDATE track_prefs SET itunes_rating = NULL, itunes_plays = NULL, itunes_last_played = NULL")
        tx.execute("DELETE FROM itunes_playlist_items")
        tx.execute("DELETE FROM itunes_playlists")
    settings.save({"itunes_enabled": False, "itunes_status": {}}, db_path)


def status(db_path=None) -> dict:
    s = settings.load(db_path)
    p = xml_path(db_path)
    return {"enabled": s.get("itunes_enabled", True), "use_rating": s.get("itunes_use_rating", True),
            "xml": str(p) if p else None, "exists": bool(p and p.exists()), "default_dir": str(default_dir()),
            "last": s.get("itunes_status") or {}}


def maybe_reimport(db_path=None) -> bool:
    """XML が前回の読み込みから変わっていれば読み直す（連携が有効なときだけ）。"""
    s = settings.load(db_path)
    if not s.get("itunes_enabled", True):
        return False
    p = xml_path(db_path)
    if p is None or not p.exists():
        last = s.get("itunes_status") or {}
        if last.get("error") != "not_found":
            import_xml(None, db_path)                # 「見つからない」を記録しておく（画面の案内に使う）
        return False
    last = s.get("itunes_status") or {}
    if last.get("mtime") == p.stat().st_mtime and last.get("path") == str(p) and not last.get("error"):
        return False
    import_xml(p, db_path)
    return True


def start_watch(interval: float = 300, db_path=None) -> None:
    """起動時と、interval 秒ごとに XML の更新を見る（裏のスレッド）。"""
    def loop():
        while True:
            try:
                maybe_reimport(db_path)
            except Exception:
                log.exception("itunes watch")
            time.sleep(interval)
    threading.Thread(target=loop, name="itunes-watch", daemon=True).start()


def playlists(db_path=None) -> list[dict]:
    """プレイリストの一覧（フォルダの形。曲数と解析済みの数は設定の音楽フォルダの曲だけ数える）。"""
    from .roots import where as roots_where
    rw, rargs = roots_where("t", db_path)
    c = db.connect(db_path)
    cnt = {r[0]: (r[1], r[2] or 0) for r in c.execute(
        f"SELECT i.playlist_id, COUNT(*), SUM(t.analysis_status = 'done') FROM itunes_playlist_items i JOIN tracks t ON t.id = i.track_id "
        f"WHERE {rw} GROUP BY i.playlist_id", rargs)}
    return [{"id": r["id"], "pid": r["pid"], "name": r["name"], "parent": r["parent_pid"], "folder": bool(r["is_folder"]),
             "smart": bool(r["is_smart"]), "count": cnt.get(r["id"], (0, 0))[0], "analyzed": cnt.get(r["id"], (0, 0))[1]}
            for r in c.execute("SELECT * FROM itunes_playlists ORDER BY sort")]


def playlist_tracks(ids: list[int], db_path=None) -> set[int]:
    if not ids:
        return set()
    c = db.connect(db_path)
    return {r[0] for r in c.execute(f"SELECT DISTINCT track_id FROM itunes_playlist_items WHERE playlist_id IN ({','.join('?' * len(ids))})", ids)}
