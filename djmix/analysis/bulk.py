"""ライブラリのまとめて解析（design_phase3.md §3.3）。

別プロセス（最大 4 本、優先度は「通常以下」）で 1 曲ずつ解析し、キャッシュを書く。DB の更新と要約（おまかせ用）は親が行う。
- 自動では始めない。start() で始め、pause() で止める（解析中の曲は終わるまで待つ）。アプリを閉じると止まり、次は未解析の曲から
- どちらかのデッキが再生中は、同時に解析する曲を 1 曲に減らす（set_playing）
- 範囲（ジャンルのまとまり）を指定すると、その曲を先に解析する。rest=False ならその範囲だけ
- デッキ・キューからの解析依頼（worker.py）とは、同じ曲を同時に解析しないよう「預かり」で調整する
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

from .. import db
from ..planner import desc
from ..planner.genres import group_of
from . import cache, cutpoints, sections
from .features import BAR_COLUMNS

log = logging.getLogger("djmix.bulk")

MIN_SEC, MAX_SEC = 30, 1200      # 短すぎる・長すぎる（20 分超）曲はおまかせに使わないので、まとめて解析では飛ばす
# 見張り（2026-10-01: 一晩の解析が GPU の停止で 7 時間止まったまま・子プロセスが落ちて打ち切られた）
TASK_TIMEOUT = 300               # 1 曲がこの秒数を超えたら止まったとみなし、子プロセスを作り直す（最長 20 分の曲で通常 10〜40 秒）
TASK_TIMEOUT_STEMS = 900         # パートの分析もするとき
MAX_STRIKES = 2                  # 作り直しに 2 回巻き込まれた曲は「失敗」にする（曲のせいかもしれないので）
MAX_REBUILDS = 6                 # 1 曲も終わらないまま作り直しがこの回数続いたら、まとめて解析を止める
POOL_LOST = "A process in the process pool was terminated abruptly"


# ---- 子プロセス ----
def _child_init(gpu_lock=None) -> None:
    from ..procutil import ensure_std_streams
    ensure_std_streams()                # コンソールなしで起動したアプリの子プロセス（pythonw）でも落ちないように
    from . import gpu
    gpu.set_process_lock(gpu_lock)      # GPU を使う処理は子プロセスの間で 1 本ずつ（gpu.py）
    try:        # Windows: 優先度を「通常以下」にして、再生や画面の操作を邪魔しない
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)       # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


def _child_analyze(tid: int, path: str, with_stems: bool = False) -> dict:
    import os
    from .pipeline import analyze_file
    t0 = time.time()
    # 試験用: 見張り（止まった・落ちた子プロセスの作り直し）を試す
    if os.environ.get("DJMIX_TEST_HANG") and os.environ["DJMIX_TEST_HANG"] in path:
        time.sleep(3600)
    if os.environ.get("DJMIX_TEST_CRASH") and os.environ["DJMIX_TEST_CRASH"] in path:
        os._exit(1)
    a, wave, feats, dprob = analyze_file(path)
    cache.write(tid, a, wave, feats, dprob)
    if with_stems:                     # パートの量（音源分離。Phase 4 B1）
        from . import stems
        from ..audio.decode import decode
        from ..audio.decode import SR as _SR
        stems.save(tid, stems.compute(decode(path), _SR, [b["time"] for b in a["bars"]], a["duration"]))
    return {"bpm": a["bpm"], "camelot": a["key"]["camelot"], "lufs": a["lufs"], "beat_class": a["beat_class"],
            "sec": round(time.time() - t0, 2)}


# ---- 親 ----

def _keep_awake(on: bool) -> None:
    """一晩のまとめて解析の途中でスリープしないように（この処理のスレッドが動いている間だけ有効。Windows 以外は何もしない）。"""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass

class BulkAnalyzer:
    def __init__(self, worker, db_path=None, max_procs: int = 4):
        self.worker = worker              # AnalysisWorker（預かりと、画面への完了の知らせに使う）
        self.db_path = db_path
        self.max_procs = max_procs
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.ex: ProcessPoolExecutor | None = None
        self.stop_flag = threading.Event()
        self.playing = False
        self.state = {"running": False, "groups": [], "rest": True, "stems": False, "done": 0, "failed": 0, "started": None,
                      "rate": None, "in_flight": 0, "error": None}

    # -- 操作 --
    def start(self, groups: list[str] | None = None, rest: bool = True, stems: bool = False) -> bool:
        with self.lock:
            if self.state["running"]:
                self.state.update(groups=groups or [], rest=rest)      # 実行中なら範囲だけ差し替える（次に選ぶ曲から効く）
                return False
            self.stop_flag.clear()
            self.state.update(running=True, groups=groups or [], rest=rest, stems=bool(stems), done=0, failed=0, started=time.time(),
                              rate=None, error=None, restarts=0, closing=False)
        self.thread = threading.Thread(target=self._run, name="bulk-analysis", daemon=True)
        self.thread.start()
        return True

    def pause(self) -> None:
        self.stop_flag.set()

    def shutdown(self) -> None:
        """アプリの終了時: 解析中の曲を待たずに子プロセスを止める（その曲は次回、未解析から）。"""
        self.stop_flag.set()
        with self.lock:
            self.state["closing"] = True
        self._kill_pool(self.ex)

    def set_playing(self, playing: bool) -> None:
        self.playing = bool(playing)

    def status(self) -> dict:
        c = db.connect(self.db_path)
        from ..library.roots import where as roots_where
        rw, rargs = roots_where("", self.db_path)
        elig = f"duration BETWEEN {MIN_SEC} AND {MAX_SEC} AND {rw}"
        total, analyzed, failed = c.execute(
            f"SELECT COUNT(*), SUM(analysis_status = 'done'), SUM(analysis_status = 'failed') FROM tracks WHERE {elig}", rargs).fetchone()
        with self.lock:
            st = dict(self.state)
        left = left_all = (total or 0) - (analyzed or 0) - (failed or 0)
        if st["running"] and st["groups"] and not st["rest"]:          # 範囲だけ解析するときは、残りもその範囲で数える
            gs = set(st["groups"])
            left = sum(1 for r in c.execute(f"SELECT genre FROM tracks WHERE analysis_status IN ('none', 'running') AND {elig}", rargs)
                       if group_of(r["genre"]) in gs)
        rate = st["rate"]
        procs = 1 if self.playing else self.max_procs
        st.update(total=total or 0, analyzed=analyzed or 0, failed_total=failed or 0, left=left, left_all=left_all, playing=self.playing,
                  procs=procs, eta_sec=round(left / rate) if rate else None)
        return st

    # -- 本体 --
    def _next_ids(self, n: int, skip: set[int]) -> list[tuple[int, str]]:
        """未解析の曲を n 曲。範囲（ジャンルのまとまり）の曲を先に。"""
        with self.lock:
            groups, rest = set(self.state["groups"]), self.state["rest"]
        c = db.connect(self.db_path)
        from ..library.roots import where as roots_where
        rw, rargs = roots_where("", self.db_path)
        rows = c.execute(f"SELECT id, path, genre FROM tracks WHERE analysis_status = 'none' AND duration BETWEEN {MIN_SEC} AND {MAX_SEC} AND {rw} "
                         "ORDER BY folder, path", rargs).fetchall()
        first, later = [], []
        for r in rows:
            if r["id"] in skip:
                continue
            (first if not groups or group_of(r["genre"]) in groups else later).append((r["id"], r["path"]))
            if len(first) >= n:
                break
        pick = first + (later if rest else [])
        return pick[:n]

    def failed_list(self, limit: int = 500) -> list[dict]:
        """解析できなかった曲（設定の音楽フォルダの、まとめて解析の対象の長さのもの）。"""
        from ..library.roots import where as roots_where
        rw, rargs = roots_where("", self.db_path)
        c = db.connect(self.db_path)
        return [dict(r) for r in c.execute(
            f"SELECT id, title, artist, album, path, analysis_error AS error FROM tracks WHERE analysis_status = 'failed' "
            f"AND duration BETWEEN {MIN_SEC} AND {MAX_SEC} AND {rw} ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, path LIMIT ?",
            (*rargs, limit))]

    def retry_failed(self) -> int:
        """解析できなかった曲を未解析に戻す（次のまとめて解析で、もう一度解析する）。"""
        from ..library.roots import where as roots_where
        rw, rargs = roots_where("", self.db_path)
        with db.tx(self.db_path) as c:
            return c.execute(f"UPDATE tracks SET analysis_status = 'none', analysis_error = NULL WHERE analysis_status = 'failed' "
                             f"AND duration BETWEEN {MIN_SEC} AND {MAX_SEC} AND {rw}", rargs).rowcount

    def recover(self) -> int:
        """起動時: 子プロセスが落ちたせいで「失敗」になった曲（曲のせいではない）を未解析に戻す。"""
        with db.tx(self.db_path) as c:
            n = c.execute("UPDATE tracks SET analysis_status = 'none', analysis_error = NULL "
                          "WHERE analysis_status = 'failed' AND analysis_error LIKE ?", (POOL_LOST + "%",)).rowcount
        if n:
            log.info("bulk analysis: %d tracks failed by a lost worker were reset to unanalyzed", n)
        return n

    def _new_pool(self) -> ProcessPoolExecutor:
        ctx = mp.get_context("spawn")
        # ロックは作り直すたびに新しく（止まった子プロセスが握ったままのことがある）
        return ProcessPoolExecutor(max_workers=self.max_procs, mp_context=ctx, initializer=_child_init, initargs=(ctx.Lock(),))

    @staticmethod
    def _kill_pool(ex: ProcessPoolExecutor | None) -> None:
        if ex is None:
            return
        for p in list((getattr(ex, "_processes", None) or {}).values()):
            try:
                p.terminate()
            except Exception:
                pass
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _run(self) -> None:  # noqa: C901
        ex = None
        pending: dict[Future, tuple[int, float]] = {}
        t_hist: list[float] = []
        strikes: dict[int, int] = {}
        rebuilds = 0                   # 最後に 1 曲終わってからの作り直しの回数
        _keep_awake(True)              # 解析の間は Windows をスリープさせない（画面は消えてよい）

        def rebuild(reason: str, suspects: set[int]) -> None:
            """子プロセスを止めて作り直す。解析中だった曲は未解析に戻す（suspects は疑いが濃い曲。空なら全部）。2 回で失敗に。"""
            nonlocal ex, rebuilds
            rebuilds += 1
            log.warning("bulk analysis: restarting workers (%s), in flight %s", reason, [v[0] for v in pending.values()])
            self._kill_pool(ex)
            for tid, _ in pending.values():
                if not suspects or tid in suspects:
                    strikes[tid] = strikes.get(tid, 0) + 1
                self.worker.release(tid)
                if strikes.get(tid, 0) >= MAX_STRIKES:
                    self._set(tid, "failed", analysis_error=f"解析中に止まりました（{reason}）")
                    with self.lock:
                        self.state["failed"] += 1
                    self.worker.notify({"id": tid, "status": "failed", "error": reason})
                else:
                    self._set(tid, "none")
            pending.clear()
            with self.lock:
                self.state["restarts"] = self.state.get("restarts", 0) + 1
            if rebuilds >= MAX_REBUILDS:
                ex = self.ex = None
                raise RuntimeError(f"解析の子プロセスが続けて止まるため中断しました（{reason}）。GPU の状態を確かめてから「続きから」を押してください")
            ex = self.ex = self._new_pool()

        try:
            ex = self.ex = self._new_pool()
            while True:
                limit = 1 if self.playing else (min(2, self.max_procs) if self.state["stems"] else self.max_procs)   # 分離もするなら GPU メモリのため 2 本
                timeout = TASK_TIMEOUT_STEMS if self.state["stems"] else TASK_TIMEOUT
                if not self.stop_flag.is_set():
                    need = limit - len(pending)
                    if need > 0:
                        for tid, path in self._next_ids(need, {v[0] for v in pending.values()}):
                            if not self.worker.claim(tid):         # デッキ・キューの解析が先に扱っている
                                continue
                            self._set(tid, "running")
                            pending[ex.submit(_child_analyze, tid, path, self.state["stems"])] = (tid, time.time())
                if not pending:
                    break              # 止めた、または解析する曲が残っていない
                with self.lock:
                    self.state["in_flight"] = len(pending)
                done, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
                if any(isinstance(f.exception(), BrokenProcessPool) for f in done):
                    if self.state.get("closing"):
                        break          # アプリの終了で子プロセスを止めた（finally で未解析に戻す）
                    rebuild("子プロセスが落ちた", set())
                    continue
                for f in done:
                    tid, _ = pending.pop(f)
                    self._finish(tid, f)
                    rebuilds = 0
                    t_hist.append(time.time())
                    t_hist[:] = [t for t in t_hist if t > time.time() - 120]      # 直近 2 分の速さ
                    with self.lock:
                        span = time.time() - max(self.state["started"], t_hist[0] - 1) if t_hist else 0
                        self.state["rate"] = round(len(t_hist) / span, 3) if span > 20 else None      # 最初の数曲は同時に終わって速く見えるので 20 秒は測る
                late = {tid for f, (tid, t0) in pending.items() if time.time() - t0 > timeout}
                if late:
                    rebuild(f"1 曲が {timeout} 秒を超えた", late)
        except Exception as e:
            log.exception("bulk analysis stopped")
            with self.lock:
                self.state["error"] = str(e)[:300]
        finally:
            _keep_awake(False)
            for f, (tid, _) in pending.items():        # 途中の曲は未解析に戻す（次回その曲から）
                self.worker.release(tid)
                self._set(tid, "none")
            if self.state.get("closing"):
                self._kill_pool(ex)
            elif ex is not None:
                ex.shutdown(wait=False, cancel_futures=True)
            self.ex = None
            with self.lock:
                self.state.update(running=False, in_flight=0)
            log.info("bulk analysis ended: done %d, failed %d, restarts %d", self.state["done"], self.state["failed"], self.state.get("restarts", 0))

    def _finish(self, tid: int, f: Future) -> None:
        try:
            r = f.result()
            a = cache.read(tid)
            feats = cache.bars_array(tid, len(BAR_COLUMNS))
            sections.forget(tid)
            cutpoints.forget(tid)
            if a is not None and feats is not None:
                desc.update(tid, a, feats, self.db_path)
            self._set(tid, "done", analysis_version=cache.ANALYSIS_VERSION, analysis_error=None,
                      bpm=r["bpm"], camelot=r["camelot"], lufs=r["lufs"], beat_class=r["beat_class"])
            with self.lock:
                self.state["done"] += 1
            result = {"id": tid, "status": "done"}
        except Exception as e:
            log.warning("bulk analysis failed for %d: %s", tid, e)
            self._set(tid, "failed", analysis_error=str(e)[:500])
            with self.lock:
                self.state["failed"] += 1
            result = {"id": tid, "status": "failed", "error": str(e)[:200]}
        self.worker.release(tid)
        self.worker.notify(result)

    def _set(self, tid: int, status: str, **cols) -> None:
        sets = ", ".join(["analysis_status = ?"] + [f"{k} = ?" for k in cols])
        with db.tx(self.db_path) as c:
            c.execute(f"UPDATE tracks SET {sets} WHERE id = ?", [status, *cols.values(), tid])
