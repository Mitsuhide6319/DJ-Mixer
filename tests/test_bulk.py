"""P3-3: まとめて解析（design_phase3.md §3.3）。一時フォルダの合成クリック曲 3 曲を別プロセスで解析する。"""
import os
import time

from djmix import db
from djmix.analysis import bulk, cache
from djmix.analysis.worker import AnalysisWorker
from tests.test_analysis import click_track


def test_bulk_analyzes_range_first_and_skips_claimed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("DJMIX_DATA", str(data))              # 子プロセスのキャッシュも一時フォルダへ
    monkeypatch.setattr(cache, "CACHE_DIR", data / "cache")
    dbp = data / "t.sqlite"
    c = db.connect(dbp)
    for i, (genre, bpm) in enumerate([("Pop", 110), ("Jazz", 120), ("Rock", 130)], 1):
        p = tmp_path / f"{i}.wav"
        click_track(p, bpm=bpm, seconds=40)
        c.execute("INSERT INTO tracks(id, path, folder, title, genre, duration) VALUES(?, ?, 'x', ?, ?, 40)", (i, str(p), f"t{i}", genre))
    c.commit()
    w = AnalysisWorker(dbp)
    assert w.claim(3)                                         # 3 曲目はデッキの解析が扱っている、という想定
    b = bulk.BulkAnalyzer(w, dbp, max_procs=1)       # 1 本なら終わる順＝選ぶ順
    b.start(["ジャズ・フュージョン"], rest=True)
    t0 = time.time()
    while b.status()["running"] and time.time() - t0 < 180:
        time.sleep(0.5)
    rows = {r["id"]: r["analysis_status"] for r in c.execute("SELECT id, analysis_status FROM tracks")}
    assert rows[1] == rows[2] == "done" and rows[3] == "none", rows
    assert b.status()["done"] == 2 and b.status()["failed"] == 0
    assert c.execute("SELECT COUNT(*) FROM track_desc").fetchone()[0] == 2
    assert [r["id"] for r in w.status()["recent"]] == [2, 1]  # 範囲（ジャズ）の曲が先。画面にも知らせる
    assert (data / "cache" / "1" / "analysis.json").exists()


def _setup(tmp_path, monkeypatch, names):
    data = tmp_path / "data"
    monkeypatch.setenv("DJMIX_DATA", str(data))
    monkeypatch.setattr(cache, "CACHE_DIR", data / "cache")
    dbp = data / "t.sqlite"
    c = db.connect(dbp)
    for i, n in enumerate(names, 1):
        p = tmp_path / f"{n}.wav"
        click_track(p, bpm=120, seconds=35)
        c.execute("INSERT INTO tracks(id, path, folder, title, genre, duration) VALUES(?, ?, 'x', ?, 'Jazz', 35)", (i, str(p), n))
    c.commit()
    return dbp, c


def _wait(b, sec=240):
    t0 = time.time()
    while b.status()["running"] and time.time() - t0 < sec:
        time.sleep(0.5)


def test_bulk_restarts_hung_worker_and_continues(tmp_path, monkeypatch):
    """1 曲が止まったまま（2026-10-01 の GPU の停止を想定）→ 時間切れで子プロセスを作り直し、他の曲は続ける。止まる曲は 2 回で失敗に。"""
    dbp, c = _setup(tmp_path, monkeypatch, ["a_ok", "b_hang", "c_ok"])
    monkeypatch.setenv("DJMIX_TEST_HANG", "b_hang")
    monkeypatch.setattr(bulk, "TASK_TIMEOUT", 20)
    b = bulk.BulkAnalyzer(AnalysisWorker(dbp), dbp, max_procs=2)
    b.start()
    _wait(b)
    rows = {r["title"]: (r["analysis_status"], r["analysis_error"]) for r in c.execute("SELECT title, analysis_status, analysis_error FROM tracks")}
    assert rows["a_ok"][0] == rows["c_ok"][0] == "done", rows
    assert rows["b_hang"][0] == "failed" and "止まりました" in rows["b_hang"][1]
    assert b.status()["restarts"] == 2 and b.status()["error"] is None


def test_bulk_recovers_from_crashed_worker(tmp_path, monkeypatch):
    """子プロセスが落ちた（ドライバのリセットなど）→ 作り直して続ける。巻き込まれた曲は失敗にせず、落とす曲だけ 2 回で失敗に。起動時の recover。"""
    dbp, c = _setup(tmp_path, monkeypatch, ["a_ok", "b_crash", "c_ok"])
    monkeypatch.setenv("DJMIX_TEST_CRASH", "b_crash")
    b = bulk.BulkAnalyzer(AnalysisWorker(dbp), dbp, max_procs=1)
    b.start()
    _wait(b)
    rows = {r["title"]: r["analysis_status"] for r in c.execute("SELECT title, analysis_status FROM tracks")}
    assert rows == {"a_ok": "done", "b_crash": "failed", "c_ok": "done"}, rows
    # 前の版で「子プロセスが落ちた」ために失敗になった曲は、起動時に未解析へ戻す
    with db.tx(dbp) as t:
        t.execute("UPDATE tracks SET analysis_status = 'failed', analysis_error = ? WHERE title = 'c_ok'",
                  (bulk.POOL_LOST + " while the future was running or pending.",))
    assert b.recover() == 1
    assert c.execute("SELECT analysis_status FROM tracks WHERE title = 'c_ok'").fetchone()[0] == "none"


def test_failed_list_and_retry(tmp_path, monkeypatch):
    dbp, c = _setup(tmp_path, monkeypatch, ["a", "b"])
    with db.tx(dbp) as t:
        t.execute("UPDATE tracks SET analysis_status = 'failed', analysis_error = 'decode error' WHERE title = 'b'")
    b = bulk.BulkAnalyzer(AnalysisWorker(dbp), dbp)
    assert [(x["title"], x["error"]) for x in b.failed_list()] == [("b", "decode error")]
    assert b.retry_failed() == 1
    assert c.execute("SELECT analysis_status FROM tracks WHERE title = 'b'").fetchone()[0] == "none"
