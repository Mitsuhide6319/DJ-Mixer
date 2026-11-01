"""1 曲の解析（design_phase1.md §3.3）。"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..audio.decode import SR, decode
from . import bars as bars_mod
from . import beats as beats_mod
from . import features


def analyze_file(path: str | Path) -> tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
    """戻り値: (analysis.json の内容, 波形 (3,n) uint8, 小節特徴量 (bars, cols) float32, ダウンビート確率 50fps)。"""
    t0 = time.time()
    audio = decode(path)
    duration = len(audio) / SR
    mono = audio.mean(axis=1)

    det = beats_mod.detect(mono, SR)
    raw_b, raw_d, bprob, dprob = det["beats"], det["downbeats"], det["beat_prob"], det["down_prob"]
    t_detect = time.time()

    b, octave_fixes = beats_mod.fix_octave(raw_b, bprob)
    b = beats_mod.smooth(b)
    nobeat = beats_mod.no_beat_regions(b, bprob, duration)
    lt = beats_mod.local_tempo(b)
    valid = lt[np.isfinite(lt)]
    bpm = float(np.median(valid)) if len(valid) else 0.0
    n = len(valid)
    head = float(np.median(valid[: max(1, n // 5)])) if n else 0.0
    tail = float(np.median(valid[-max(1, n // 5):])) if n else 0.0
    drift = (tail - head) / head * 100 if head else 0.0

    strength = bars_mod.downbeat_strength(b, dprob)
    meter, meter_scores = bars_mod.estimate_meter(strength)
    bar_list = bars_mod.bar_phases(b, strength, meter)

    lufs, peak = features.loudness(audio, SR)
    wave = features.waveform(mono, SR)
    feats, keys = features.bar_features(mono, SR, [x["time"] for x in bar_list], duration)
    extra = features.track_extra(mono, SR)

    m = beats_mod.raw_metrics(raw_b, raw_d, bprob)
    no_beat_total = sum(e - s for s, e in nobeat)
    cls, reasons = beats_mod.classify(m, no_beat_total, drift)

    analysis = {
        "duration": round(duration, 3), "sample_rate": SR,
        "bpm": round(bpm, 2), "bpm_p5": round(float(np.percentile(valid, 5)), 2) if n else None,
        "bpm_p95": round(float(np.percentile(valid, 95)), 2) if n else None, "drift_pct": round(drift, 2),
        "beats": np.round(b, 4).tolist(), "beats_raw": np.round(raw_b, 3).tolist(),
        "downbeats_raw": np.round(raw_d, 3).tolist(),
        "meter": meter, "meter_scores": meter_scores, "bars": bar_list,
        "no_beat": nobeat, "no_beat_total": round(no_beat_total, 1),
        "tempo_curve": {"t": np.round(b, 3).tolist(), "bpm": np.round(np.nan_to_num(lt, nan=0), 2).tolist()},
        "octave_fixes": octave_fixes,
        "lufs": round(lufs, 2), "peak_db": round(peak, 2),
        "key": keys["all"], "key_head30": keys["head30"], "key_tail30": keys["tail30"],
        "beat_class": cls, "beat_class_reasons": reasons, "metrics": {k: round(v, 4) for k, v in m.items()},
        "wave": {"rate": features.WAVE_RATE, "bands": ["low", "mid", "high"], "length": int(wave.shape[1])},
        "bar_columns": features.BAR_COLUMNS,
        "extra": extra,
        "timing": {"detect_s": round(t_detect - t0, 2), "total_s": round(time.time() - t0, 2)},
    }
    return analysis, wave, feats, dprob
