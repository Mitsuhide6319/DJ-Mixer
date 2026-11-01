"""音楽フォルダのタグ走査（design_phase1.md §3.1）。拍・キー等の解析はしない（ユーザー回答「使う曲だけ解析」）。

変更検出は (path, size, mtime)。変わっていない曲はタグを読み直さない。消えた曲は DB から削除する。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import db
from ..config import MUSIC_ROOT
from .textfix import fix_mojibake

log = logging.getLogger("djmix.scan")

AUDIO_EXT = {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".aif", ".aiff", ".ogg", ".opus", ".wma"}


def _first(tags, *keys):
    for k in keys:
        v = tags.get(k) if tags else None
        if v:
            return fix_mojibake(str(v[0] if isinstance(v, list) else v).strip()) or None
    return None


def read_tags(path: Path) -> dict:
    """タグと長さを読む。読めなければファイル名・フォルダ名で補う。"""
    import mutagen

    info = {"title": None, "artist": None, "album": None, "genre": None, "year": None, "duration": None}
    try:
        f = mutagen.File(path, easy=True)
        if f is not None:
            t = f.tags
            info["title"] = _first(t, "title")
            info["artist"] = _first(t, "artist", "albumartist")
            info["album"] = _first(t, "album")
            info["genre"] = _first(t, "genre")
            info["year"] = _first(t, "date", "year")
            if getattr(f, "info", None) is not None and getattr(f.info, "length", None):
                info["duration"] = float(f.info.length)
    except Exception as e:  # 壊れたファイルでも一覧には出す
        log.debug("tag read failed %s: %s", path, e)
    info["title"] = info["title"] or path.stem
    info["album"] = info["album"] or path.parent.name
    return info


class Scanner:
    """バックグラウンドでの走査と進捗。音楽フォルダは複数（I-3）。見つからないフォルダは飛ばし、その曲は消さない。"""

    def __init__(self, roots=None, db_path=None):
        self.db_path = db_path
        self.set_roots(roots)
        self.lock = threading.Lock()
        self.state = {"running": False, "found": 0, "done": 0, "added": 0, "updated": 0, "removed": 0,
                      "started": None, "finished": None, "error": None, "offline": []}

    def set_roots(self, roots) -> None:
        if roots is None:
            roots = [MUSIC_ROOT] if MUSIC_ROOT else []
        elif isinstance(roots, (str, Path)):
            roots = [roots]
        self.roots = [Path(r) for r in roots]

    @property
    def root(self) -> Path:               # 互換（Phase 1 は音楽フォルダが 1 つだった）
        return self.roots[0] if self.roots else (Path(MUSIC_ROOT) if MUSIC_ROOT else None)

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, found=0, done=0, added=0, updated=0, removed=0,
                              started=time.time(), finished=None, error=None, offline=[])
        threading.Thread(target=self._run, name="scanner", daemon=True).start()
        return True

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _inc(self, key, n=1):
        with self.lock:
            self.state[key] += n

    def _run(self):
        try:
            self.scan()
        except Exception as e:
            log.exception("scan failed")
            self._set(error=str(e))
        finally:
            self._set(running=False, finished=time.time())

    def list_files(self) -> tuple[list[tuple[Path, Path, int, float]], list[str]]:
        """(音楽フォルダ, パス, 大きさ, 更新時刻) の一覧と、見つからなかった音楽フォルダ。同じファイルは最初の音楽フォルダだけに数える。"""
        files, offline, seen = [], [], set()
        for root in self.roots:
            if not root.exists():
                offline.append(str(root))
                continue
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    if os.path.splitext(n)[1].lower() in AUDIO_EXT:
                        p = Path(dirpath) / n
                        if str(p) in seen:
                            continue
                        seen.add(str(p))
                        try:
                            st = p.stat()
                        except OSError:
                            continue
                        files.append((root, p, st.st_size, st.st_mtime))
        return files, offline

    def scan(self) -> None:
        t0 = time.time()
        files, offline = self.list_files()
        self._set(found=len(files), offline=offline)
        if offline:
            log.warning("music folders not found (skipped, their tracks are kept): %s", offline)
        if not files and len(offline) == len(self.roots):
            raise FileNotFoundError(f"音楽フォルダが見つかりません: {', '.join(offline)}")
        scanned = [str(r) for r in self.roots if str(r) not in offline]
        conn = db.connect(self.db_path)
        rows = conn.execute("SELECT path, size, mtime, root FROM tracks").fetchall()
        known = {r["path"]: (r["size"], r["mtime"], r["root"]) for r in rows}
        todo = [(root, p, s, m) for root, p, s, m in files if known.get(str(p), (None, None, None))[:2] != (s, m)
                or known[str(p)][2] != str(root)]
        self._inc("done", len(files) - len(todo))

        def work(item):
            root, p, s, m = item
            same = known.get(str(p), (None, None, None))[:2] == (s, m)
            return root, p, s, m, (None if same else read_tags(p))

        batch = []
        # タグ読みは I/O 待ちが主なのでスレッドで並列化する
        with ThreadPoolExecutor(max_workers=8) as ex:
            for root, p, s, m, info in ex.map(work, todo):
                rel = p.relative_to(root)
                folder = rel.parts[0] if len(rel.parts) > 1 else ""
                if info is None:                       # ファイルは同じで、音楽フォルダだけ変わった（記録を付け直す）
                    with db.tx(self.db_path) as c:
                        c.execute("UPDATE tracks SET root = ?, folder = ? WHERE path = ?", (str(root), folder, str(p)))
                    self._inc("done")
                    continue
                batch.append((str(p), folder, s, m, info["title"], info["artist"], info["album"],
                              info["genre"], info["year"], info["duration"], str(root), str(p) in known))
                if len(batch) >= 200:
                    self._flush(conn, batch)
                    batch = []
            self._flush(conn, batch)

        # 消すのは、走査した音楽フォルダの曲でファイルが無くなったものだけ（外したフォルダ・見つからないフォルダの曲は残す）
        present = {str(p) for _, p, _, _ in files}
        gone = [k for k, (_, _, r) in known.items() if k not in present and r in scanned]
        with db.tx(self.db_path) as c:
            c.executemany("DELETE FROM tracks WHERE path = ?", [(g,) for g in gone])
        self._inc("removed", len(gone))
        log.info("scan finished: %d files, %d new/changed, %d removed, %.1fs", len(files), len(todo), len(gone), time.time() - t0)

    def _flush(self, conn, batch):
        if not batch:
            return
        with db.tx(self.db_path) as c:
            c.executemany(
                """INSERT INTO tracks(path, folder, size, mtime, title, artist, album, genre, year, duration, root)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET folder=excluded.folder, size=excluded.size, mtime=excluded.mtime, root=excluded.root,
                     title=excluded.title, artist=excluded.artist, album=excluded.album, genre=excluded.genre,
                     year=excluded.year, duration=excluded.duration,
                     -- ファイルが変わったので解析結果は無効（ユーザー修正 user_edits は残す）
                     analysis_status='none', analysis_version=NULL, bpm=NULL, camelot=NULL, lufs=NULL, beat_class=NULL""",
                [b[:11] for b in batch])
        upd = sum(1 for b in batch if b[11])
        self._inc("updated", upd)
        self._inc("added", len(batch) - upd)
        self._inc("done", len(batch))
