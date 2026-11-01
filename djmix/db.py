"""SQLite（design_phase1.md §3.2）。解析結果（自動）とユーザー修正（user_edits）は別テーブルに分けて保存する。

接続はスレッドごとに持つ（FastAPI の同期エンドポイントはスレッドプールで動くため）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  folder TEXT NOT NULL,              -- 音楽フォルダ直下のフォルダ名（ライブラリのツリー用）
  size INTEGER, mtime REAL,
  title TEXT, artist TEXT, album TEXT, genre TEXT, year TEXT,
  duration REAL,
  analysis_status TEXT NOT NULL DEFAULT 'none',   -- none / queued / running / done / failed
  analysis_version INTEGER,
  analysis_error TEXT,
  bpm REAL, camelot TEXT, lufs REAL, beat_class TEXT
);
CREATE INDEX IF NOT EXISTS ix_tracks_folder ON tracks(folder);
CREATE INDEX IF NOT EXISTS ix_tracks_status ON tracks(analysis_status);
CREATE TABLE IF NOT EXISTS user_edits(
  track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  meter INTEGER,
  bpm_scale REAL NOT NULL DEFAULT 1,
  downbeat_anchors TEXT,
  grid_offset_ms REAL NOT NULL DEFAULT 0,
  hotcues TEXT, cue_point REAL, memo TEXT
);
CREATE TABLE IF NOT EXISTS queues(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_items(
  queue_id INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  use_start REAL, use_end REAL, use_label TEXT,  -- 使用区間（NULL=フル）。編集は Phase 2 のつなぎ目エディタ
  PRIMARY KEY(queue_id, pos)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS track_prefs(      -- 曲の好み（Phase 4。曲の走査で上書きされないよう tracks と別の表）
  track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL DEFAULT 0,          -- 0 = 未評価、1〜5
  play_count INTEGER NOT NULL DEFAULT 0,
  last_played REAL
);
CREATE TABLE IF NOT EXISTS itunes_playlists(  -- iTunes のプレイリスト（Phase 5 E。読み込むたびに入れ替える）
  id INTEGER PRIMARY KEY,
  pid TEXT,                                   -- iTunes の Persistent ID
  name TEXT NOT NULL,
  parent_pid TEXT,                            -- 親のフォルダ
  is_folder INTEGER NOT NULL DEFAULT 0,
  is_smart INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0             -- iTunes の並び
);
CREATE TABLE IF NOT EXISTS itunes_playlist_items(
  playlist_id INTEGER NOT NULL REFERENCES itunes_playlists(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_itpl_items ON itunes_playlist_items(playlist_id, pos);
CREATE TABLE IF NOT EXISTS plays(            -- 再生の履歴（Phase 4）
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  at REAL NOT NULL,
  source TEXT NOT NULL                        -- deck / mix / export
);
CREATE INDEX IF NOT EXISTS ix_plays_at ON plays(at);
CREATE TABLE IF NOT EXISTS track_desc(       -- おまかせ用の要約（Phase 3。解析キャッシュから作り直せる）
  track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  data TEXT NOT NULL
);
"""

# 後から追加した列（古い DB にも起動時に追加する）
ADDED_COLUMNS = {
    "tracks": [
        ("root", "TEXT"),             # どの音楽フォルダの曲か（使ってみての改善 I-3。外したフォルダの曲は隠す）
    ],
    "track_prefs": [                  # iTunes から取り込んだ値（Phase 5 E。アプリの値とは別に持つ）
        ("itunes_rating", "INTEGER"),
        ("itunes_plays", "INTEGER"),
        ("itunes_last_played", "REAL"),
    ],
    "queues": [                       # ミックス（名前付きの保存したキュー。Phase 4）
        ("kind", "TEXT"),             # 'auto' おまかせ / 'hand' 手で作った
        ("created", "REAL"),
        ("plays", "INTEGER NOT NULL DEFAULT 0"),
        ("last_played", "REAL"),
        ("exports", "TEXT"),          # JSON: [{path, format, seconds, at, tracklist}]
    ],
    "queue_items": [
        ("join_next", "INTEGER"),     # このつなぎが対象とする次の曲（Phase 2。並べ替えで変わったら標準に戻す）
        ("join_bars", "INTEGER"),     # 重なりの小節数
        ("join_tpl", "TEXT"),         # 繋ぎ方
        ("join_params", "TEXT"),      # 細かい調整（JSON）
    ],
}


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    # タグの文字化けを一度だけ直す（Phase 3。design_phase3.md §2。ファイルのタグは書き換えない）
    if c.execute("SELECT 1 FROM settings WHERE key = 'tags_fixed_v1'").fetchone() is None:
        from .library.textfix import fix_mojibake
        cols = ("title", "artist", "album", "genre")
        rows = c.execute(f"SELECT id, {', '.join(cols)} FROM tracks").fetchall()
        for r in rows:
            fixed = [fix_mojibake(r[k]) for k in cols]
            if fixed != [r[k] for k in cols]:
                c.execute(f"UPDATE tracks SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?", [*fixed, r["id"]])
        c.execute("INSERT INTO settings(key, value) VALUES('tags_fixed_v1', 'true')")
    c.commit()


_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    key = str(path)
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    if key not in conns:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        with _init_lock:
            if key not in _initialized:
                c.executescript(SCHEMA)
                _migrate(c)
                _initialized.add(key)
        conns[key] = c
    return conns[key]


@contextmanager
def tx(path: Path | None = None):
    c = connect(path)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
