"""解析キャッシュ data/cache/<track_id>/（design_phase1.md §3.4）。

ANALYSIS_VERSION を上げると、古いキャッシュは次に使うとき再解析される。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from ..config import CACHE_DIR

ANALYSIS_VERSION = 3  # 2: 波形の点の間隔を正確に 1/200 秒にした / 3: 曲全体の特徴量 extra（Phase 3 のおまかせ用）


def track_dir(track_id: int) -> Path:
    return CACHE_DIR / str(track_id)


def write(track_id: int, analysis: dict, wave: np.ndarray, bars: np.ndarray, down_prob: np.ndarray) -> None:
    d = track_dir(track_id)
    tmp = d.with_name(d.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    (tmp / "analysis.json").write_text(json.dumps({"version": ANALYSIS_VERSION, **analysis}, ensure_ascii=False),
                                       encoding="utf-8")
    wave.astype(np.uint8).tofile(tmp / "wave.u8")
    bars.astype(np.float32).tofile(tmp / "bars.f32")
    down_prob.astype(np.float16).tofile(tmp / "downbeat.f16")
    # 途中で落ちても壊れたキャッシュが残らないよう、一時フォルダに書いてから入れ替える
    if d.exists():
        shutil.rmtree(d)
    tmp.rename(d)


def read(track_id: int) -> dict | None:
    p = track_dir(track_id) / "analysis.json"
    if not p.exists():
        return None
    a = json.loads(p.read_text(encoding="utf-8"))
    return a if a.get("version") == ANALYSIS_VERSION else None


def wave_bytes(track_id: int) -> bytes | None:
    p = track_dir(track_id) / "wave.u8"
    return p.read_bytes() if p.exists() else None


def bars_array(track_id: int, ncols: int) -> np.ndarray | None:
    p = track_dir(track_id) / "bars.f32"
    return np.fromfile(p, dtype=np.float32).reshape(-1, ncols) if p.exists() else None


def downbeat_prob(track_id: int) -> np.ndarray | None:
    p = track_dir(track_id) / "downbeat.f16"
    return np.fromfile(p, dtype=np.float16).astype(np.float32) if p.exists() else None
