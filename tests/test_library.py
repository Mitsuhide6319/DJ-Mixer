"""P1-2: ライブラリ走査・検索・ミックスキュー。一時フォルダに小さな WAV を作って試す。"""
import os
import time

import numpy as np
import pytest
import soundfile as sf

from djmix import queues
from djmix.library import query
from djmix.library.scanner import Scanner


@pytest.fixture()
def lib(tmp_path):
    root = tmp_path / "music"
    for folder, names in {"Jazz": ["So What", "Blue Train"], "House": ["Get Lucky"]}.items():
        (root / folder).mkdir(parents=True)
        for n in names:
            sf.write(root / folder / f"{n}.wav", np.zeros((4410, 2), dtype=np.float32), 44100)
    (root / "Jazz" / "cover.jpg").write_bytes(b"x")  # 音声以外は無視される
    dbp = tmp_path / "t.sqlite"
    from djmix import settings
    settings.save({"music_roots": [str(root)]}, dbp)
    return root, dbp


def test_scan_and_search(lib):
    root, dbp = lib
    s = Scanner(root, dbp)
    s.scan()
    st = s.status()
    assert st["found"] == 3 and st["added"] == 3
    assert query.counts(dbp) == {"all": 3, "analyzed": 0}
    r = query.search("blue", db_path=dbp)
    assert r["total"] == 1 and r["items"][0]["title"] == "Blue Train"
    assert r["items"][0]["album"] == "Jazz"            # タグが無ければフォルダ名で補う
    assert abs(r["items"][0]["duration"] - 0.1) < 0.01
    assert {f["folder"]: f["count"] for f in query.folders(dbp)} == {"House": 1, "Jazz": 2}
    assert query.search("", folder="House", db_path=dbp)["total"] == 1


def test_rescan_is_incremental_and_removes_missing(lib):
    root, dbp = lib
    Scanner(root, dbp).scan()
    s2 = Scanner(root, dbp)
    s2.scan()
    assert s2.status()["added"] == 0 and s2.status()["updated"] == 0
    (root / "House" / "Get Lucky.wav").unlink()
    p = root / "Jazz" / "So What.wav"
    sf.write(p, np.zeros((8820, 2), dtype=np.float32), 44100)
    os.utime(p, (time.time() + 5, time.time() + 5))
    s3 = Scanner(root, dbp)
    s3.scan()
    assert s3.status()["removed"] == 1 and s3.status()["updated"] == 1
    assert query.counts(dbp)["all"] == 2


def test_queue_roundtrip(lib):
    root, dbp = lib
    Scanner(root, dbp).scan()
    ids = [t["id"] for t in query.search("", sort="title", db_path=dbp)["items"]]
    queues.save([{"track_id": i} for i in ids], db_path=dbp)
    got = queues.load(db_path=dbp)
    assert [g["track_id"] for g in got] == ids and got[0]["track"]["title"] == "Blue Train"
    queues.save([{"track_id": ids[2], "use_label": "テーマだけ"}, {"track_id": ids[0]}], name="set1", db_path=dbp)
    assert [q["name"] for q in queues.names(dbp)] == ["set1"]
    s1 = queues.load("set1", db_path=dbp)
    assert s1[0]["use_label"] == "テーマだけ" and len(s1) == 2
    assert len(queues.load(db_path=dbp)) == 3          # 作業中のキューは別に残る


def test_joins_follow_the_next_track(lib):
    """P2-2: つなぎは次の曲とセット。並べ替えで次の曲が変わったら標準に戻る。"""
    root, dbp = lib
    Scanner(root, dbp).scan()
    a, b, c = [t["id"] for t in query.search("", sort="title", db_path=dbp)["items"]]
    items = [{"track_id": a, "use_start": 10.0, "use_end": 90.0, "use_label": "テーマ",
              "join_next": b, "join_bars": 8, "join_tpl": "echo", "join_params": {"swapAt": 0.3}},
             {"track_id": b, "join_next": c, "join_bars": 32, "join_tpl": "long"},
             {"track_id": c, "join_next": None, "join_bars": 16}]
    queues.save(items, db_path=dbp)
    got = queues.load(db_path=dbp)
    assert got[0]["join_bars"] == 8 and got[0]["join_params"] == {"swapAt": 0.3} and got[0]["use_label"] == "テーマ"
    assert got[2]["join_bars"] is None                        # 最後の曲のつなぎは無い
    # b と c を入れ替えると、a→c・c→b は別の組み合わせなので標準に戻る
    queues.save([items[0], items[2], items[1]], db_path=dbp)
    got = queues.load(db_path=dbp)
    assert [g["join_bars"] for g in got] == [None, None, None]
    assert got[0]["use_start"] == 10.0                         # 使う範囲は曲に付くので残る


def test_old_db_gets_new_columns(tmp_path):
    """Phase 1 の DB（つなぎの列が無い）でも起動時に列が追加される。"""
    import sqlite3
    from djmix import db as dbm
    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE queue_items(queue_id INTEGER, pos INTEGER, track_id INTEGER, use_start REAL, use_end REAL, use_label TEXT, PRIMARY KEY(queue_id,pos));")
    c.close()
    cols = {r[1] for r in dbm.connect(p).execute("PRAGMA table_info(queue_items)")}
    assert {"join_next", "join_bars", "join_tpl", "join_params"} <= cols


# ---- 音楽フォルダを複数に（使ってみての改善 I-3） ----
from djmix import db, settings  # noqa: E402
from djmix.library import roots  # noqa: E402


@pytest.fixture()
def two(tmp_path):
    dbp = tmp_path / "t.sqlite"
    ra, rb = tmp_path / "a", tmp_path / "b"
    for r, names in ((ra, ["One", "Two"]), (rb, ["Three"])):
        (r / "F").mkdir(parents=True)
        for n in names:
            sf.write(r / "F" / f"{n}.wav", np.zeros((4410, 2), dtype=np.float32), 44100)
    settings.save({"music_roots": [str(ra), str(rb)]}, dbp)
    return ra, rb, dbp


def test_multi_roots_scan_and_filter(two):
    ra, rb, dbp = two
    Scanner(roots.configured(dbp), dbp).scan()
    assert query.counts(dbp)["all"] == 3
    assert {(f["root"], f["count"]) for f in query.folders(dbp)} == {(str(ra), 2), (str(rb), 1)}
    assert query.search("", root=str(rb), db_path=dbp)["total"] == 1


def test_removed_root_is_hidden_not_deleted(two):
    ra, rb, dbp = two
    Scanner(roots.configured(dbp), dbp).scan()
    tid = query.search("Three", db_path=dbp)["items"][0]["id"]
    db.connect(dbp).execute("INSERT INTO track_prefs(track_id, rating) VALUES(?, 5)", (tid,))
    settings.save({"music_roots": [str(ra)]}, dbp)
    s = Scanner(roots.configured(dbp), dbp)
    s.scan()
    assert s.status()["removed"] == 0
    assert query.counts(dbp)["all"] == 2                      # 隠れる
    assert db.connect(dbp).execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3   # 消えていない
    settings.save({"music_roots": [str(ra), str(rb)]}, dbp)  # 戻すと ★ ごと見える
    r = query.search("Three", db_path=dbp)["items"][0]
    assert r["id"] == tid and r["rating"] == 5


def test_offline_root_keeps_tracks(two, tmp_path):
    ra, rb, dbp = two
    Scanner(roots.configured(dbp), dbp).scan()
    moved = tmp_path / "unplugged"
    rb.rename(moved)                                         # 外付けドライブが外れた
    s = Scanner(roots.configured(dbp), dbp)
    s.scan()
    assert s.status()["removed"] == 0 and s.status()["offline"] == [str(rb)]
    assert query.counts(dbp)["all"] == 3
    ra.rename(tmp_path / "gone_too")                         # 全部見つからなければエラー（何も消さない）
    with pytest.raises(FileNotFoundError):
        Scanner(roots.configured(dbp), dbp).scan()
    assert db.connect(dbp).execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3


def test_backfill_root_for_old_rows(two):
    ra, rb, dbp = two
    Scanner(roots.configured(dbp), dbp).scan()
    with db.tx(dbp) as c:
        c.execute("UPDATE tracks SET root = NULL")
    assert roots.backfill(dbp) == 3
    assert {r[0] for r in db.connect(dbp).execute("SELECT DISTINCT root FROM tracks")} == {str(ra), str(rb)}


def test_sort_desc_and_nulls_last(lib):
    root, dbp = lib
    Scanner(root, dbp).scan()
    titles = [t["title"] for t in query.search("", sort="title", desc=True, db_path=dbp)["items"]]
    assert titles == ["So What", "Get Lucky", "Blue Train"]
    with db.tx(dbp) as c:
        c.execute("UPDATE tracks SET bpm = 120, camelot = '10A' WHERE title = 'So What'")
        c.execute("UPDATE tracks SET bpm = 90, camelot = '2B' WHERE title = 'Blue Train'")
    for d in (False, True):
        ids = [t["title"] for t in query.search("", sort="bpm", desc=d, db_path=dbp)["items"]]
        assert ids[-1] == "Get Lucky"                         # 値の無い曲は昇順でも降順でも最後
    assert [t["title"] for t in query.search("", sort="camelot", db_path=dbp)["items"]][:2] == ["Blue Train", "So What"]


def test_artist_album_facets_and_filter(lib):
    root, dbp = lib
    Scanner(root, dbp).scan()
    with db.tx(dbp) as c:
        c.execute("UPDATE tracks SET artist = 'Miles Davis' WHERE title = 'So What'")
        c.execute("UPDATE tracks SET artist = 'John Coltrane' WHERE title = 'Blue Train'")
    arts = {a["name"]: a["count"] for a in query.facets("artist", db_path=dbp)}
    assert arts == {"John Coltrane": 1, "Miles Davis": 1, "": 1}          # タグ無しは "" （最後）
    assert query.facets("artist", db_path=dbp)[-1]["name"] == ""
    albs = {a["name"]: (a["count"], a["artists"]) for a in query.facets("album", db_path=dbp)}
    assert albs == {"House": (1, 1), "Jazz": (2, 2)}
    assert [a["name"] for a in query.facets("album", artist="Miles Davis", db_path=dbp)] == ["Jazz"]
    assert query.search("", artist="Miles Davis", db_path=dbp)["total"] == 1
    assert query.search("", artist="", db_path=dbp)["items"][0]["title"] == "Get Lucky"
    assert query.search("", album="Jazz", artist="John Coltrane", db_path=dbp)["total"] == 1


def test_lib_view_setting_persists(tmp_path):
    dbp = tmp_path / "t.sqlite"
    v = {"sort": {"key": "artist", "desc": True}, "widths": {"title": 420}, "mode": "album"}
    settings.save({"lib_view": v}, dbp)
    assert settings.load(dbp)["lib_view"] == v
    settings.save({"lib_view": "bad"}, dbp)
    assert settings.load(dbp)["lib_view"] == v


def test_auto_cond_and_export_pref_persist(tmp_path):
    dbp = tmp_path / "t.sqlite"
    settings.save({"auto_cond": {"minutes": 60, "range": "playlists", "playlists": [3]}, "export_pref": {"format": "mp3_320"}}, dbp)
    s = settings.load(dbp)
    assert s["auto_cond"]["minutes"] == 60 and s["export_pref"]["format"] == "mp3_320"
    settings.save({"auto_cond": "bad"}, dbp)
    assert settings.load(dbp)["auto_cond"]["minutes"] == 60


def test_search_filters_bpm_key_genre_rating_partial(lib):
    """起点の曲を選ぶ小窓の絞り込み: アーティスト・アルバムの部分一致、BPM の範囲、キー、ジャンルのまとまり、★。"""
    from djmix import prefs
    root, dbp = lib
    Scanner(root, dbp).scan()
    with db.tx(dbp) as c:
        c.execute("UPDATE tracks SET artist='Miles Davis', bpm=120, camelot='8A', genre='Jazz' WHERE title='So What'")
        c.execute("UPDATE tracks SET artist='John Coltrane', bpm=95, camelot='9B', genre='Jazz' WHERE title='Blue Train'")
        c.execute("UPDATE tracks SET artist='Daft Punk', bpm=116, camelot='8A', genre='House' WHERE title='Get Lucky'")
    t = lambda **kw: sorted(x["title"] for x in query.search("", db_path=dbp, **kw)["items"])
    assert t(artist_q="miles") == ["So What"]
    assert t(album_q="jaz") == ["Blue Train", "So What"]                 # アルバムはフォルダ名（Jazz）
    assert t(bpm_min=100, bpm_max=118) == ["Get Lucky"]
    assert t(keys=["8A"]) == ["Get Lucky", "So What"]
    assert t(groups=["ジャズ・フュージョン"]) == ["Blue Train", "So What"]
    sw = query.search("So What", db_path=dbp)["items"][0]["id"]
    prefs.set_rating(sw, 4, dbp)
    assert t(rating_min=4) == ["So What"] and t(rating_min=5) == []


def test_album_list_and_tracks(lib):
    """おまかせの「アルバムで選ぶ」（2026-10-25）: アルバムごとの曲数・解析済みの曲数、選んだアルバムの解析済みの曲。"""
    from djmix import db
    root, dbp = lib
    Scanner(root, dbp).scan()
    with db.tx(dbp) as c:
        c.execute("UPDATE tracks SET analysis_status = 'done' WHERE title = 'So What'")
    al = {a["name"]: a for a in query.album_list(db_path=dbp)}
    assert al["Jazz"]["count"] == 2 and al["Jazz"]["analyzed"] == 1 and al["House"]["analyzed"] == 0
    assert [a["name"] for a in query.album_list("jaz", db_path=dbp)] == ["Jazz"]
    assert [t["title"] for t in query.album_tracks(["Jazz", "House"], db_path=dbp)] == ["So What"]
