"""アプリの設定（SQLite の settings テーブル。値は JSON）。"""
from __future__ import annotations

import json

from . import db
from .config import MUSIC_ROOT

DEFAULTS = {
    "output_device": "",          # AudioContext.setSinkId に渡すデバイス ID（空 = 既定のデバイス）
    "lufs_target": -14.0,         # ラウドネス補正の目標
    "tempo_range": 0.08,          # テンポフェーダーの幅（0.08 / 0.16 / 0.5）
    "xfade_curve": "smooth",
    "queue_visible": True,
    "queue_autoload": True,       # 曲が終わったデッキにキューの次の曲を自動で読み込む
    "tempo_return": True,         # 繋ぎの後、主になった曲の速度を 8 小節かけて原速へ戻す（Phase 2）
    "prefer_rating": True,        # おまかせ: ★ の多い曲を優先（Phase 4）
    "skip_rating1": True,         # おまかせ: ★1 の曲は使わない
    "avoid_recent_days": 14,      # おまかせ: 最近 N 日にかけた曲は避ける（0 = 避けない）
    "current_mix": None,          # キューをどのミックスから開いたか（書き出し・通し再生をそのミックスに記録する）
    "music_root": str(MUSIC_ROOT) if MUSIC_ROOT else "",
    "music_roots": [],            # 音楽フォルダ（複数。空なら music_root 1 つ。I-3）
    "midi_mapping": "default",
    "itunes_xml": "",             # iTunes のライブラリ XML（空なら既定の場所を探す。Phase 5 E）
    "itunes_enabled": True,       # iTunes の情報を取り込む
    "itunes_use_rating": True,    # アプリの ★ が無い曲は iTunes の ★ を使う
    "itunes_status": {},          # 前回の読み込み（件数・時刻・XML の更新時刻・エラー）
    "prefer_plays": True,         # おまかせ: よく聴く曲を少し優先（アプリ＋iTunes の再生回数）
    "auto_parts": True,           # おまかせ: パートごとに鳴らす繋ぎを使う（Phase 5 B2）
    "lib_view": {},
    "auto_cond": {},              # おまかせの条件（出発点・起点の曲・範囲・長さ・流れ）。専用ウィンドウは localStorage が再起動で消えるのでここに
    "export_pref": {},            # 書き出しの設定（方式・形式・保存先・曲目リスト）               # ライブラリ一覧の見た目（並べ替え・列の幅・左の一覧の種類。I-1/I-4）。専用ウィンドウは localStorage が残らないのでここに
}


def load(db_path=None) -> dict:
    out = dict(DEFAULTS)
    for k, v in db.connect(db_path).execute("SELECT key, value FROM settings"):
        if k in DEFAULTS:
            try:
                out[k] = json.loads(v)
            except ValueError:
                pass
    return out


def save(patch: dict, db_path=None) -> dict:
    cur = load(db_path)
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        if k == "lufs_target":
            v = max(-23.0, min(-8.0, float(v)))
        if k == "tempo_range" and float(v) not in (0.08, 0.16, 0.5):
            continue
        if k == "xfade_curve" and v not in ("smooth", "linear", "cut"):
            continue
        if k == "music_roots":
            v = [str(x).strip() for x in (v or []) if str(x).strip()]
        if k in ("lib_view", "auto_cond", "export_pref") and not isinstance(v, dict):
            continue
        if k == "avoid_recent_days":
            v = max(0, min(365, int(v or 0)))
        if k in ("itunes_status",) and not isinstance(v, dict):
            continue
        if k in ("queue_visible", "queue_autoload", "tempo_return", "prefer_rating", "skip_rating1",
                 "itunes_enabled", "itunes_use_rating", "prefer_plays", "auto_parts"):
            v = bool(v)
        cur[k] = v
    with db.tx(db_path) as c:
        c.executemany("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      [(k, json.dumps(cur[k], ensure_ascii=False)) for k in patch if k in DEFAULTS])
    return cur
