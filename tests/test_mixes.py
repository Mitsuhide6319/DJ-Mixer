"""P4-1: ミックスの保存・好み・履歴（design_phase4.md §1）。"""
import time

from djmix import db, mixes, prefs, queues
from djmix.library import query


def setup(tmp_path):
    dbp = tmp_path / "t.sqlite"
    c = db.connect(dbp)
    for i in range(1, 6):
        c.execute("INSERT INTO tracks(id, path, folder, title, duration, bpm, analysis_status) VALUES(?, ?, 'x', ?, 240, 120, 'done')",
                  (i, f"{i}.mp3", f"t{i}"))
    c.commit()
    items = [{"track_id": i, "use_start": 10.0, "use_end": 200.0, "join_tpl": "long", "join_bars": 16, "join_next": i + 1} for i in (1, 2, 3)]
    queues.save(items, queues.CURRENT, dbp)
    return dbp


def test_save_list_rename_duplicate_delete(tmp_path):
    dbp = setup(tmp_path)
    m = mixes.save(queues.load(queues.CURRENT, dbp), "夜のドライブ", "auto", dbp)
    assert m["kind"] == "auto" and m["count"] == 3
    assert abs(m["minutes"] - (190 * 3 - 2 * 32) / 60) < 0.2           # 使う範囲の合計 − 重なり（16 小節 × 2）
    m2 = mixes.save(queues.load(queues.CURRENT, dbp), "夜のドライブ", "hand", dbp)
    assert m2["name"] == "夜のドライブ (2)"                              # 同じ名前は上書きしない
    lst = mixes.list_mixes(db_path=dbp)
    assert [x["name"] for x in lst] == ["夜のドライブ (2)", "夜のドライブ"] and all(x["name"] != queues.CURRENT for x in lst)
    assert mixes.rename(m["id"], "朝のテーマ", dbp)["name"] == "朝のテーマ"
    d = mixes.duplicate(m["id"], dbp)
    assert d["name"] == "朝のテーマ のコピー" and d["count"] == 3
    assert mixes.delete(d["id"], dbp) and mixes.get(d["id"], dbp) is None


def test_record_export_attaches_or_creates(tmp_path):
    dbp = setup(tmp_path)
    m = mixes.save(queues.load(queues.CURRENT, dbp), "A", "hand", dbp)
    r = mixes.record_export({"path": r"C:\x\A.mp3", "format": "mp3_320", "seconds": 600}, m["id"], dbp)
    assert r["id"] == m["id"] and len(r["exports"]) == 1                # キューがミックスのまま → そのミックスに記録
    items = queues.load(queues.CURRENT, dbp)[:2]
    queues.save(items, queues.CURRENT, dbp)                             # キューを変えた
    r2 = mixes.record_export({"path": r"C:\x\別の名前.wav", "format": "wav", "seconds": 300}, m["id"], dbp)
    assert r2["id"] != m["id"] and r2["name"] == "別の名前" and r2["count"] == 2
    assert prefs.recent(1, dbp) == {1, 2, 3}                            # 書き出した曲は「かけた」


def test_ratings_and_history(tmp_path):
    dbp = setup(tmp_path)
    prefs.set_rating(2, 5, dbp); prefs.set_rating(3, 1, dbp); prefs.set_rating(3, 9, dbp)
    assert prefs.ratings(dbp) == {2: 5, 3: 5}
    old = time.time() - 30 * 86400
    prefs.record_plays([4], "deck", at=old, db_path=dbp)
    assert prefs.record_plays([1, 1, 2], "mix", db_path=dbp) == 2
    assert prefs.record_plays([1], "mix", db_path=dbp) == 0             # 10 分以内の重複は記録しない
    assert prefs.recent(14, dbp) == {1, 2} and prefs.recent(60, dbp) == {1, 2, 4}
    r = {t["id"]: t for t in query.search("", db_path=dbp)["items"]}
    assert r[2]["rating"] == 5 and r[5]["rating"] == 0 and r[1]["last_played"] is not None
