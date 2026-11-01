"""P3-1: タグの文字化けの修正（design_phase3.md §2）。"""
import sqlite3

from djmix import db
from djmix.library.textfix import fix_mojibake


def sjis_as_latin1(s: str) -> str:
    return s.encode("cp932").decode("latin-1")


def test_fix_mojibake():
    for ja in ["安藤まさひろ", "運命の夜-piano ver.-", "ウッソ・エヴィン", "トラック 12"]:
        assert fix_mojibake(sjis_as_latin1(ja)) == ja
    # 正しい名前は変えない（アクセント付きの欧文・日本語・英語・空）
    for ok in ["Céline Dion", "Björk", "Sigur Rós", "Mötley Crüe", "Déjà Vu", "Prajñà", "Far East Café",
               "太陽のメロディー", "So What", "", None]:
        assert fix_mojibake(ok) == ok


def test_existing_db_fixed_once(tmp_path):
    dbp = tmp_path / "old.sqlite"
    c = sqlite3.connect(dbp)
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO tracks(path, folder, title, artist) VALUES('a.mp3', 'x', ?, ?)", (sjis_as_latin1("待ちぼうけの午後"), "Björk"))
    c.commit(); c.close()
    r = db.connect(dbp).execute("SELECT title, artist FROM tracks").fetchone()
    assert (r["title"], r["artist"]) == ("待ちぼうけの午後", "Björk")
    assert db.connect(dbp).execute("SELECT value FROM settings WHERE key = 'tags_fixed_v1'").fetchone() is not None
