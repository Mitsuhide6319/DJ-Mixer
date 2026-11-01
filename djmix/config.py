"""パス・設定値。値は環境変数で上書きできる（テストや別環境用）。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = Path(os.environ.get("DJMIX_DATA", ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "library.sqlite"
MAPPINGS_DIR = ROOT / "config" / "mappings"
# 既定の音楽フォルダ。無ければ未設定（はじめての起動で選んでもらう。2026-11-01、友人と共有するため G:\MUSIC の既定をやめた）
MUSIC_ROOT = Path(os.environ["DJMIX_MUSIC_ROOT"]) if os.environ.get("DJMIX_MUSIC_ROOT") else None

APP_TITLE = "DJ Mixer"
HOST = "127.0.0.1"
