"""GPU の使い方（2026-10-01 一晩のまとめて解析の停止を受けて）。

GPU は 8 GB。まとめて解析の子プロセス 4 本がそれぞれ拍の検出（Beat This!）で最大約 1.1 GB を抱えたままにすると、
アプリ本体の分だけで GPU のメモリが埋まり、ドライバが応答しなくなる（nvlddmkm 13/153、解析が止まったまま）。
- GPU を使う処理は、まとめて解析の子プロセスの間で 1 本ずつ（プロセスをまたぐロック）。GPU の部分は 1 曲 0.1〜1 秒なので遅くならない
- 使い終わるたびに、PyTorch が抱えている空きメモリを返す（empty_cache）
"""
from __future__ import annotations

from contextlib import contextmanager

_lock = None          # まとめて解析の子プロセスでは multiprocessing の Lock（親が渡す）。それ以外は None


def set_process_lock(lock) -> None:
    global _lock
    _lock = lock


def release_cache() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


@contextmanager
def use():
    """GPU を使う区間。この中で結果を CPU へ移してから抜けること。"""
    if _lock is not None:
        _lock.acquire()
    try:
        yield
    finally:
        release_cache()
        if _lock is not None:
            _lock.release()
