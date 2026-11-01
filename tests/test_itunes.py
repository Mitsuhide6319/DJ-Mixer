"""Phase 5 E: iTunes の情報の取り込み（design_phase5.md §E）。iTunes と同じ形の XML を作って試す。"""
import datetime as dt
import plistlib
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest
import soundfile as sf

from djmix import db, prefs, settings
from djmix.library import itunes, query
from djmix.library.scanner import Scanner


def loc(p: Path) -> str:
    return "file://localhost/" + quote(str(p).replace("\\", "/"))


@pytest.fixture()
def lib(tmp_path):
    root = tmp_path / "Music Lib"
    (root / "Fusion").mkdir(parents=True)
    names = ["Asayake", "Truth", "Looking Up", "Omens of Love"]
    for n in names:
        sf.write(root / "Fusion" / f"{n}.wav", np.zeros((4410, 2), dtype=np.float32), 44100)
    dbp = tmp_path / "t.sqlite"
    settings.save({"music_roots": [str(root)]}, dbp)
    Scanner(root, dbp).scan()
    ids = {t["title"]: t["id"] for t in query.search("", db_path=dbp)["items"]}
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    tracks = {
        "101": {"Track ID": 101, "Name": "Asayake", "Location": loc(root / "Fusion" / "Asayake.wav"), "Rating": 100, "Play Count": 87,
                "Play Date UTC": now - dt.timedelta(days=2)},
        "102": {"Track ID": 102, "Name": "Truth", "Location": loc(root / "Fusion" / "Truth.wav").replace("Fusion", "FUSION"),  # 大文字小文字違い
                "Rating": 80, "Play Count": 5, "Play Date UTC": now - dt.timedelta(days=40)},
        "103": {"Track ID": 103, "Name": "Looking Up", "Location": loc(root / "Fusion" / "Looking Up.wav"), "Rating": 60, "Rating Computed": True},
        "104": {"Track ID": 104, "Name": "Gone", "Location": loc(root / "Fusion" / "Gone.wav"), "Rating": 40},       # ライブラリに無い
    }
    pls = [
        {"Name": "ライブラリ", "Master": True, "Playlist Items": [{"Track ID": 101}]},
        {"Name": "ミュージック", "Distinguished Kind": 4, "Playlist Items": [{"Track ID": 101}]},
        {"Name": "Fusion", "Folder": True, "Playlist Persistent ID": "F1"},
        {"Name": "Friday Night", "Playlist Persistent ID": "P1", "Parent Persistent ID": "F1",
         "Playlist Items": [{"Track ID": 102}, {"Track ID": 101}, {"Track ID": 104}]},
        {"Name": "★4 以上", "Playlist Persistent ID": "S1", "Smart Info": b"x", "Playlist Items": [{"Track ID": 101}, {"Track ID": 102}]},
    ]
    xml = tmp_path / "iTunes Music Library.xml"
    with open(xml, "wb") as f:
        plistlib.dump({"Major Version": 1, "Tracks": tracks, "Playlists": pls}, f)
    settings.save({"itunes_xml": str(xml)}, dbp)
    return dbp, ids, xml


def test_location_to_path():
    assert itunes.location_to_path("file://localhost/C:/Music/a%20b%20%E3%81%82.mp3") == r"C:\Music\a b あ.mp3"
    assert itunes.location_to_path("file://server/share/x.mp3") == r"\\server\share\x.mp3"
    assert itunes.location_to_path("http://example.com/x.mp3") is None


def test_import_ratings_plays_playlists(lib):
    dbp, ids, xml = lib
    st = itunes.import_xml(db_path=dbp)
    assert st["error"] is None and st["matched"] == 3 and st["rated"] == 2 and st["played"] == 2 and st["playlists"] == 2
    rows = {t["title"]: t for t in query.search("", db_path=dbp)["items"]}
    assert (rows["Asayake"]["rating"], rows["Asayake"]["rating_src"], rows["Asayake"]["plays"]) == (5, "itunes", 87)
    assert rows["Truth"]["rating"] == 4                                  # 大文字小文字が違うパスでも対応
    assert rows["Looking Up"]["rating"] == 0                             # アルバムの ★ から自動で付いたものは使わない
    assert rows["Asayake"]["last_itunes"] == 1 and rows["Asayake"]["last_played"] > time.time() - 3 * 86400
    # アプリの ★ が優先。消すと iTunes の ★ に戻る
    prefs.set_rating(ids["Asayake"], 2, dbp)
    assert prefs.ratings(dbp)[ids["Asayake"]] == 2
    r = query.get_tracks([ids["Asayake"]], dbp)[ids["Asayake"]]
    assert (r["rating"], r["rating_src"], r["rating_app"]) == (2, "app", 2)
    prefs.set_rating(ids["Asayake"], 0, dbp)
    assert prefs.ratings(dbp)[ids["Asayake"]] == 5
    # iTunes の ★ を使わない設定
    settings.save({"itunes_use_rating": False}, dbp)
    assert ids["Asayake"] not in prefs.ratings(dbp)
    settings.save({"itunes_use_rating": True}, dbp)
    # 最近かけた曲に iTunes の再生も入る（2 日前 ○、40 日前 ×）
    assert ids["Asayake"] in prefs.recent(14, dbp) and ids["Truth"] not in prefs.recent(14, dbp)
    assert prefs.play_counts(dbp) == {ids["Asayake"]: 87, ids["Truth"]: 5}
    # プレイリスト: 標準のもの（ライブラリ・ミュージック）は除く、フォルダの形、スマートの印、曲の順
    pl = {p["name"]: p for p in itunes.playlists(dbp)}
    assert set(pl) == {"Fusion", "Friday Night", "★4 以上"}
    assert pl["Fusion"]["folder"] and pl["Friday Night"]["parent"] == "F1" and pl["★4 以上"]["smart"]
    assert pl["Friday Night"]["count"] == 2
    got = query.search("", playlist=pl["Friday Night"]["id"], db_path=dbp)
    assert [t["title"] for t in got["items"]] == ["Truth", "Asayake"] and got["total"] == 2
    assert itunes.playlist_tracks([pl["★4 以上"]["id"]], dbp) == {ids["Asayake"], ids["Truth"]}
    # 並べ替え: 再生回数
    assert [t["title"] for t in query.search("", sort="plays", desc=True, db_path=dbp)["items"]][:2] == ["Asayake", "Truth"]


def test_reimport_on_change_and_unlink(lib):
    dbp, ids, xml = lib
    assert itunes.maybe_reimport(dbp) is True
    assert itunes.maybe_reimport(dbp) is False                           # 変わっていなければ読まない
    pl = plistlib.loads(xml.read_bytes())
    pl["Tracks"]["101"]["Rating"] = 20
    xml.write_bytes(plistlib.dumps(pl))
    import os
    os.utime(xml, (time.time() + 10, time.time() + 10))
    assert itunes.maybe_reimport(dbp) is True
    assert prefs.ratings(dbp)[ids["Asayake"]] == 1
    prefs.set_rating(ids["Truth"], 3, dbp)
    itunes.unlink(dbp)
    assert prefs.ratings(dbp) == {ids["Truth"]: 3}                        # アプリの ★ は残る
    assert itunes.playlists(dbp) == [] and itunes.maybe_reimport(dbp) is False   # 連携をやめたら読み直さない


def test_missing_xml(tmp_path):
    dbp = tmp_path / "t.sqlite"
    settings.save({"itunes_xml": str(tmp_path / "none.xml")}, dbp)
    assert itunes.import_xml(db_path=dbp)["error"] == "not_found"
    assert itunes.status(dbp)["exists"] is False


def test_planner_prefers_played_tracks(lib):
    from types import SimpleNamespace
    from djmix.planner import engine
    dbp, ids, xml = lib
    itunes.import_xml(db_path=dbp)
    pool = SimpleNamespace(d={i: {} for i in ids.values()})
    skip, bonus = engine.apply_prefs(pool, {"prefer_rating": False, "prefer_plays": True, "avoid_recent_days": 0}, dbp)
    assert bonus[ids["Asayake"]] > bonus[ids["Truth"]] > 0 and ids["Looking Up"] not in bonus
    assert abs(bonus[ids["Asayake"]] - engine.PLAYS_BONUS) < 1e-9          # いちばん聴く曲で +0.03
    _, none = engine.apply_prefs(pool, {"prefer_rating": False, "prefer_plays": False, "avoid_recent_days": 0}, dbp)
    assert none == {}
    skip, _ = engine.apply_prefs(pool, {"avoid_recent_days": 14}, dbp)
    assert ids["Asayake"] in skip                                        # iTunes で 2 日前に再生
