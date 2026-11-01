"""ffmpeg による音声デコード。解析と再生で同じデコードを使い、拍時刻とサンプル位置を一致させる
（design_phase1.md §1.1。MP3/AAC はデコーダごとにエンコーダ遅延の扱いが違うため）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np

SR = 44100


def decode(path: str | Path, sr: int = SR) -> np.ndarray:
    """曲全体を float32 ステレオに展開する。戻り値 shape=(n, 2)。"""
    from ..procutil import run
    proc = run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg でデコードできません: {path}: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2).copy()
