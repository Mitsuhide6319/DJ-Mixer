"""音源分離で小節ごとのパートの量を求める（Phase 4 B1。design_phase4.md §3.1）。

Demucs（htdemucs）で ボーカル・ドラム・ベース・その他 に分け、小節ごとの音量（RMS。曲の中で一番大きいパートの値を 1 とする）だけを
解析キャッシュ `stems.f32`（小節 × 4、列の順は SOURCES）に保存する。分離した音は保存しない（1 曲数 KB）。
GPU で 1 曲 4〜9 秒（Phase 4 の事前試験）。モデルは最初に使うときに一度だけ読み込む。

Phase 5 B2（パートごとに鳴らす）: 繋ぎに使う曲だけ、分離した音も `stems.ogg`（8ch = 4 パート × ステレオ、SOURCES の順、
Ogg Vorbis）に保存する。5 分の曲で約 20MB・書き出しに約 15 秒（design_phase5.md §B2 の事前試験）。
"""
from __future__ import annotations

import json
import logging
import os
import threading

import numpy as np

from . import cache

log = logging.getLogger("djmix.stems")

SOURCES = ["vocals", "drums", "bass", "other"]
STEMS_VERSION = 1
VOCAL_ON = 0.25          # これ以上を「ボーカルがある小節」とみなす（モックアップの試作と同じ）

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            import torch
            from demucs.pretrained import get_model
            m = get_model("htdemucs")
            m.eval()
            if torch.cuda.is_available():
                m.cuda()
            _model = m
        return _model


def separate(audio: np.ndarray) -> np.ndarray:
    """audio: (n, 2) float32 → 分離した音 (4 パート（SOURCES の順）, 2, n) float32。"""
    import torch
    from demucs.apply import apply_model
    model = _get_model()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    wav = torch.from_numpy(np.ascontiguousarray(audio.T, dtype=np.float32))
    ref = wav.mean(0)
    m, s = ref.mean(), ref.std() + 1e-8
    from . import gpu
    with torch.no_grad(), gpu.use():
        out = apply_model(model, ((wav - m) / s)[None].to(dev), device=dev, shifts=0, split=True, overlap=0.25, progress=False)[0]
        out = (out * s + m).cpu().numpy()                          # (4 パート, 2, n)
    order = [model.sources.index(k) for k in SOURCES]
    return out[order].astype(np.float32)


def levels(sep: np.ndarray, sr: int, bar_times: list[float], duration: float) -> np.ndarray:
    """分離した音 (4, 2, n) → 小節ごとのパートの量 (小節数, 4)。"""
    out = sep.mean(1)                                              # (4, n) モノラル
    order = range(len(SOURCES))
    edges = list(bar_times) + [duration]
    res = np.zeros((len(bar_times), len(SOURCES)), dtype=np.float32)
    for b, (t0, t1) in enumerate(zip(edges[:-1], edges[1:])):
        a0, a1 = int(t0 * sr), max(int(t0 * sr) + 1, int(t1 * sr))
        for c, i in enumerate(order):
            seg = out[i, a0:a1]
            res[b, c] = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
    mx = float(res.max()) or 1.0
    return res / mx


def compute(audio: np.ndarray, sr: int, bar_times: list[float], duration: float) -> np.ndarray:
    """audio: (n, 2) float32。戻り値: (小節数, 4) float32（SOURCES の順）。"""
    return levels(separate(audio), sr, bar_times, duration)


# ---- 分離した音（Phase 5 B2） ----
AUDIO_VERSION = 1


def audio_path(track_id: int):
    return cache.track_dir(track_id) / "stems.ogg"


def audio_ready(track_id: int) -> bool:
    d = cache.track_dir(track_id)
    try:
        meta = json.loads((d / "stems_audio.json").read_text(encoding="utf-8"))
        return meta.get("version") == AUDIO_VERSION and (d / "stems.ogg").exists()
    except (OSError, ValueError):
        return False


def save_audio(track_id: int, sep: np.ndarray, sr: int) -> None:
    """(4, 2, n) → stems.ogg（8ch）。書いてから名前を変える（途中で落ちても壊れたファイルを残さない）。"""
    import soundfile as sf
    d = cache.track_dir(track_id)
    tmp = d / "stems.ogg.tmp"
    x = sep.reshape(8, -1)
    with sf.SoundFile(tmp, "w", sr, 8, format="OGG", subtype="VORBIS", compression_level=0.6) as f:
        for k in range(0, x.shape[1], sr * 10):
            f.write(np.ascontiguousarray(x[:, k:k + sr * 10].T))
    os.replace(tmp, d / "stems.ogg")
    (d / "stems_audio.json").write_text(json.dumps({"version": AUDIO_VERSION, "sources": SOURCES, "sr": sr, "frames": int(x.shape[1])}),
                                        encoding="utf-8")


def read_audio(track_id: int, start: int, frames: int) -> tuple[np.ndarray, int]:
    """分離した音の一部 (8, frames) float32 と曲全体のフレーム数。"""
    import soundfile as sf
    with sf.SoundFile(audio_path(track_id)) as f:
        total = f.frames
        start = max(0, min(start, total))
        f.seek(start)
        y = f.read(min(frames, total - start), dtype="float32", always_2d=True)
    return np.ascontiguousarray(y.T), total


def compute_audio_for_track(track_id: int, path: str) -> np.ndarray | None:
    """分離して、小節ごとの量と分離した音の両方を保存する。"""
    from ..audio.decode import SR, decode
    a = cache.read(track_id)
    if a is None:
        return None
    sep = separate(decode(path))
    arr = levels(sep, SR, [b["time"] for b in a["bars"]], a["duration"])
    save(track_id, arr)
    save_audio(track_id, sep, SR)
    return arr


def save(track_id: int, arr: np.ndarray) -> None:
    d = cache.track_dir(track_id)
    arr.astype(np.float32).tofile(d / "stems.f32")
    (d / "stems.json").write_text(json.dumps({"version": STEMS_VERSION, "sources": SOURCES, "bars": int(arr.shape[0])}), encoding="utf-8")


def read(track_id: int) -> np.ndarray | None:
    d = cache.track_dir(track_id)
    try:
        meta = json.loads((d / "stems.json").read_text(encoding="utf-8"))
        if meta.get("version") != STEMS_VERSION:
            return None
        return np.fromfile(d / "stems.f32", dtype=np.float32).reshape(-1, len(SOURCES))
    except (OSError, ValueError):
        return None


def compute_for_track(track_id: int, path: str) -> np.ndarray | None:
    """解析済みの曲のパートの量を求めて保存する（解析キャッシュの小節を使う）。"""
    from ..audio.decode import SR, decode
    a = cache.read(track_id)
    if a is None:
        return None
    arr = compute(decode(path), SR, [b["time"] for b in a["bars"]], a["duration"])
    save(track_id, arr)
    return arr


# ---- 使う曲だけ、裏で 1 曲ずつ（画面から頼まれたとき・解析の直後） ----
_pending: set[int] = set()
_lock = threading.Lock()
_queue: list[tuple] = []                     # (track_id, path, on_done)
_thread: threading.Thread | None = None


def request(track_id: int, path: str, on_done=None, audio: bool = False) -> str:
    """まだならパートの量（audio=True なら分離した音も）を求めるよう頼む。戻り値: 'done' / 'pending'。
    分離した音の依頼は、量だけの依頼より先に扱う（繋ぎに使う曲なので）。"""
    global _thread
    if (audio_ready(track_id) if audio else read(track_id) is not None):
        return "done"
    with _lock:
        key = (track_id, audio)
        if key not in _pending:
            _pending.add(key)
            item = (track_id, path, on_done, audio)
            if audio:
                _queue.insert(sum(1 for q in _queue if q[3]), item)
            else:
                _queue.append(item)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="stems", daemon=True)
            _thread.start()
    return "pending"


def _run() -> None:
    while True:
        with _lock:
            if not _queue:
                return
            tid, path, on_done, audio = _queue.pop(0)
        try:
            arr = compute_audio_for_track(tid, path) if audio else compute_for_track(tid, path)
            if arr is not None and on_done:
                on_done(tid, arr)
            log.info("stems done for %d (%d bars)", tid, 0 if arr is None else len(arr))
        except Exception:
            log.exception("stems failed for %d", tid)
        finally:
            with _lock:
                _pending.discard((tid, audio))
