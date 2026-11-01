"""優先度付きの解析キュー（GPU を使うのでワーカーは 1 本）。

優先度: 0=デッキに読み込んだ曲（最優先） / 1=ミックスキューに入れた曲 / 2=ユーザーが明示的に指示した曲。
同じ曲を高い優先度で頼み直すと、その優先度で処理される。
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time

from .. import db
from . import cache, cutpoints, sections
from .pipeline import analyze_file

log = logging.getLogger("djmix.analysis")

PRIO_DECK, PRIO_QUEUE, PRIO_MANUAL = 0, 1, 2


def request_stems(track_id: int, path: str, db_path=None, audio: bool = False) -> str:
    """パートの量（音源分離）を裏で求め、終わったらおまかせ用の要約も作り直す。"""
    from ..planner import desc
    from . import stems
    from .features import BAR_COLUMNS

    def done(tid, arr):
        a, feats = cache.read(tid), cache.bars_array(tid, len(BAR_COLUMNS))
        if a is not None and feats is not None:
            desc.update(tid, a, feats, db_path, arr)
    return stems.request(track_id, path, done, audio)


class AnalysisWorker:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.want: dict[int, int] = {}      # track_id -> 依頼中の最良優先度
        self.lock = threading.Lock()
        self.seq = itertools.count()
        self.running: int | None = None
        self.recent: list[dict] = []        # 直近に終わった曲（画面の更新用）
        self.claimed: set[int] = set()      # まとめて解析（bulk.py）が預かっている曲
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="analysis", daemon=True)
            self.thread.start()

    def request(self, track_id: int, priority: int = PRIO_MANUAL, force: bool = False) -> str:
        """解析を依頼する。戻り値: 'done'（解析済み）/ 'queued' / 'running'。"""
        if not force and cache.read(track_id) is not None:
            self._set_status(track_id, "done")
            return "done"
        with self.lock:
            if self.running == track_id or track_id in self.claimed:
                return "running"
            cur = self.want.get(track_id)
            if cur is not None and cur <= priority:
                return "queued"
            self.want[track_id] = priority
            self.q.put((priority, next(self.seq), track_id))
        self._set_status(track_id, "queued")
        self.start()
        return "queued"

    # ---- まとめて解析との調整 ----
    def claim(self, track_id: int) -> bool:
        """まとめて解析がこの曲を預かる。こちらで解析中・依頼待ちなら False。"""
        with self.lock:
            if self.running == track_id or track_id in self.want or track_id in self.claimed:
                return False
            self.claimed.add(track_id)
            return True

    def release(self, track_id: int) -> None:
        with self.lock:
            self.claimed.discard(track_id)

    def notify(self, result: dict) -> None:
        """まとめて解析で終わった曲も、画面の「直近に終わった曲」に載せる。"""
        with self.lock:
            self.recent.append({**result, "t": time.time()})
            self.recent = self.recent[-200:]

    def status(self) -> dict:
        with self.lock:
            return {"pending": len(self.want), "running": self.running, "recent": list(self.recent[-50:])}

    def _set_status(self, track_id: int, status: str, **cols) -> None:
        sets = ", ".join(["analysis_status = ?"] + [f"{k} = ?" for k in cols])
        with db.tx(self.db_path) as c:
            c.execute(f"UPDATE tracks SET {sets} WHERE id = ?", [status, *cols.values(), track_id])

    def _loop(self) -> None:
        while True:
            prio, _, tid = self.q.get()
            with self.lock:
                if self.want.get(tid) != prio:   # より高い優先度で積み直された古い要素
                    continue
                del self.want[tid]
                self.running = tid
            try:
                self._analyze(tid)
            finally:
                with self.lock:
                    self.running = None

    def _analyze(self, tid: int) -> None:
        row = db.connect(self.db_path).execute("SELECT path FROM tracks WHERE id = ?", (tid,)).fetchone()
        if row is None:
            return
        self._set_status(tid, "running")
        t0 = time.time()
        try:
            a, wave, feats, dprob = analyze_file(row["path"])
            cache.write(tid, a, wave, feats, dprob)
            sections.forget(tid)      # 区間の候補を計算し直させる
            cutpoints.forget(tid)
            from ..planner import desc
            desc.update(tid, a, feats, self.db_path)      # おまかせ用の要約（Phase 3）
            request_stems(tid, row["path"], self.db_path)   # 続けてパートの量（Phase 4 B1。裏で 1 曲ずつ）
            self._set_status(tid, "done", analysis_version=cache.ANALYSIS_VERSION, analysis_error=None,
                             bpm=a["bpm"], camelot=a["key"]["camelot"], lufs=a["lufs"], beat_class=a["beat_class"])
            log.info("analyzed %s in %.1fs: %.1f BPM, meter %d, class %s", row["path"], time.time() - t0,
                     a["bpm"], a["meter"], a["beat_class"])
            result = {"id": tid, "status": "done"}
        except Exception as e:
            log.exception("analysis failed: %s", row["path"])
            self._set_status(tid, "failed", analysis_error=str(e)[:500])
            result = {"id": tid, "status": "failed", "error": str(e)[:200]}
        with self.lock:
            self.recent.append({**result, "t": time.time()})
            self.recent = self.recent[-200:]
