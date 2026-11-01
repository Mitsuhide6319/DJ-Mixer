"""再生用 PCM の配信（design_phase1.md §3.5 /api/tracks/{id}/pcm）。

解析と同じ ffmpeg のデコード結果を使うので、解析した拍時刻とサンプル位置が一致する（§1.1）。
デコード結果は直近 MAX_TRACKS 曲をメモリに保持する（9 分のステレオ float32 で約 190MB）。
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np

from .decode import SR, decode

MAX_TRACKS = 4


class PcmStore:
    def __init__(self, max_tracks: int = MAX_TRACKS):
        self.max = max_tracks
        self.data: OrderedDict[int, np.ndarray] = OrderedDict()
        self.lock = threading.Lock()
        self.loading: dict[int, threading.Event] = {}

    def get(self, track_id: int, path: str) -> np.ndarray:
        """(n, 2) float32。同じ曲の同時要求は 1 回だけデコードする。"""
        with self.lock:
            if track_id in self.data:
                self.data.move_to_end(track_id)
                return self.data[track_id]
            ev = self.loading.get(track_id)
            owner = ev is None
            if owner:
                ev = self.loading[track_id] = threading.Event()
        if not owner:
            ev.wait()
            with self.lock:
                if track_id in self.data:
                    return self.data[track_id]
            return self.get(track_id, path)
        try:
            a = decode(path)
            with self.lock:
                self.data[track_id] = a
                while len(self.data) > self.max:
                    self.data.popitem(last=False)
            return a
        finally:
            with self.lock:
                self.loading.pop(track_id, None)
            ev.set()

    def chunk(self, track_id: int, path: str, offset: int, frames: int) -> tuple[bytes, int]:
        """offset から frames サンプル分を「L 全部→R 全部」の planar float32 で返す。戻り値: (bytes, 曲の総サンプル数)。"""
        a = self.get(track_id, path)
        seg = a[max(0, offset): max(0, offset) + max(0, frames)]
        return np.ascontiguousarray(seg.T).tobytes(), len(a)


SAMPLE_RATE = SR
