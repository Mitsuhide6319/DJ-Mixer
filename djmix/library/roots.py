"""音楽フォルダ（複数。使ってみての改善 I-3。report.md 打合せ22）。

曲には、どの音楽フォルダの曲かを tracks.root に記録する。設定から外した音楽フォルダの曲は**消さずに隠す**
（ライブラリ・おまかせ・まとめて解析の対象から外す）。戻せば ★・履歴・解析結果ごと元どおりに見える。
"""
from __future__ import annotations

import os

from .. import db, settings


def norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(p).strip())) if str(p).strip() else ""


def configured(db_path=None) -> list[str]:
    """設定の音楽フォルダ（music_roots。無ければ Phase 1 からの music_root 1 つ）。"""
    s = settings.load(db_path)
    roots = [r for r in (s.get("music_roots") or []) if str(r).strip()]
    if not roots and s.get("music_root"):
        roots = [s["music_root"]]
    out, seen = [], set()
    for r in roots:
        k = norm(r)
        if k and k not in seen:
            seen.add(k)
            out.append(os.path.normpath(str(r).strip()))
    return out


def where(alias: str = "t", db_path=None) -> tuple[str, list]:
    """いま使う音楽フォルダの曲だけに絞る WHERE の部分。"""
    rs = configured(db_path)
    col = f"{alias}.root" if alias else "root"
    # root の無い曲（どの音楽フォルダの曲か分からないもの。起動時の backfill で入らなかった曲）は隠さない
    if not rs:
        return f"{col} IS NULL", []
    return f"({col} IS NULL OR {col} IN ({','.join('?' * len(rs))}))", rs


def backfill(db_path=None) -> int:
    """root の入っていない曲（音楽フォルダを複数にする前の曲）に、パスから音楽フォルダを入れる。"""
    rs = configured(db_path)
    s = settings.load(db_path)
    cands = rs + ([s["music_root"]] if s.get("music_root") and s["music_root"] not in rs else [])
    n = 0
    with db.tx(db_path) as c:
        for r in cands:
            prefix = os.path.normpath(r).rstrip("\\/") + os.sep
            n += c.execute("UPDATE tracks SET root = ? WHERE root IS NULL AND (path LIKE ? ESCAPE '!' OR path = ?)",
                           (os.path.normpath(r), prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%", r)).rowcount
    return n
