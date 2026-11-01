"""拍の検出と後処理（design_phase1.md §3.3 の 2〜5、Phase 0 の知見）。

- Beat This! の拍・小節頭 logits（50fps）
- 倍/半テンポの取り違え区間の補正（曲の途中で 2 倍/0.5 倍に入れ替わる区間だけ。曲全体の倍/半は補正しない）
- 拍時刻の平滑化（20ms 刻みの量子化を局所回帰で除去。本物のテンポ変化は残す）
- 拍なし区間（ルバート・ブレイク・無音）
"""
from __future__ import annotations

import threading

import numpy as np

FPS = 50
_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            import torch
            from beat_this.inference import Audio2Frames

            _model = Audio2Frames(checkpoint_path="final0", device="cuda" if torch.cuda.is_available() else "cpu")
        return _model


def detect(mono: np.ndarray, sr: int) -> dict:
    """Beat This! を実行する。戻り値: beats, downbeats（秒）, beat_prob, down_prob（50fps の確率）。"""
    import torch
    from beat_this.model.postprocessor import Postprocessor

    from . import gpu
    model = _get_model()
    with _model_lock, gpu.use():  # GPU は 1 本で順番に使う（まとめて解析の子プロセスの間でも。gpu.py）
        bl, dl = model(mono, sr)
        bl, dl = bl.float().cpu(), dl.float().cpu()      # GPU のメモリはここで手放す
    beats, downs = Postprocessor(type="minimal")(bl, dl)
    return {
        "beats": np.asarray(beats, dtype=np.float64),
        "downbeats": np.asarray(downs, dtype=np.float64),
        "beat_prob": torch.sigmoid(bl).numpy(),
        "down_prob": torch.sigmoid(dl).numpy(),
    }


def gap_mask(beats: np.ndarray, k: float = 1.75, min_gap: float = 1.0) -> np.ndarray:
    """ibi[i]（beats[i]→beats[i+1]）が拍の途切れ（局所中央値の k 倍超かつ min_gap 秒超）なら True。"""
    ibi = np.diff(beats)
    out = np.zeros(len(ibi), dtype=bool)
    for i, d in enumerate(ibi):
        lo, hi = max(0, i - 8), min(len(ibi), i + 9)
        local = np.median(np.r_[ibi[lo:i], ibi[i + 1:hi]]) if hi - lo > 1 else d
        out[i] = d > k * local and d > min_gap
    return out


def local_tempo(beats: np.ndarray, win: int = 8) -> np.ndarray:
    """各拍の前後 win 拍（拍の途切れはまたがない）に直線を当てはめた傾きから求めた局所 BPM。"""
    n = len(beats)
    out = np.full(n, np.nan)
    if n < 3:
        return out
    gaps = gap_mask(beats)
    seg_id = np.concatenate([[0], np.cumsum(gaps)])
    h = win // 2
    for i in range(n):
        lo, hi = max(0, i - h), min(n, i + h + 1)
        idx = np.arange(lo, hi)
        idx = idx[seg_id[idx] == seg_id[i]]
        if len(idx) < 3:
            continue
        slope = np.polyfit(idx - i, beats[idx], 1)[0]
        if slope > 0:
            out[i] = 60.0 / slope
    return out


def _runs(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    runs, start = [], None
    for i, m in enumerate(np.r_[mask, False]):
        if m and start is None:
            start = i
        elif not m and start is not None:
            if i - start >= min_len:
                runs.append((start, i))
            start = None
    return runs


def fix_octave(beats: np.ndarray, beat_prob: np.ndarray) -> tuple[np.ndarray, int]:
    """曲の中で局所テンポが曲全体の約 2 倍/約 0.5 倍になっている区間を補正する。戻り値: (拍時刻, 補正した区間数)。

    2 倍区間は 1 拍おきに間引く。残す位相は、区間の直前の拍から通常の拍間隔で続く側（なければ拍確率の高い側）。
    0.5 倍区間は拍の中点を補う。
    """
    if len(beats) < 16:
        return beats, 0
    lt = local_tempo(beats)
    ref = np.nanmedian(lt)
    ratio = lt / ref
    fixed = 0
    out = list(beats)
    # 後ろから処理すると添字がずれない
    dbl = _runs((ratio > 1.65) & (ratio < 2.35), 8)
    hlf = _runs((ratio > 0.43) & (ratio < 0.57), 4)
    edits = [("d", a, b) for a, b in dbl] + [("h", a, b) for a, b in hlf]
    for kind, a, b in sorted(edits, key=lambda e: -e[1]):
        seg = np.array(out[a:b])
        if kind == "d":
            ref_ibi = 60.0 / ref
            if a > 0:
                expect = out[a - 1] + ref_ibi
                phase = int(np.argmin([abs(seg[0] - expect), abs(seg[1] - expect)])) if len(seg) > 1 else 0
            else:
                fr = np.clip((seg * FPS).round().astype(int), 0, len(beat_prob) - 1)
                phase = int(beat_prob[fr[0::2]].sum() < beat_prob[fr[1::2]].sum())
            out[a:b] = list(seg[phase::2])
        else:
            mids = (seg[:-1] + seg[1:]) / 2
            merged = np.empty(len(seg) + len(mids))
            merged[0::2], merged[1::2] = seg, mids
            out[a:b] = list(merged)
        fixed += 1
    return np.array(out), fixed


def smooth(beats: np.ndarray, half: int = 4, keep_ms: float = 30.0) -> np.ndarray:
    """前後 half 拍の局所回帰で当てはめ直す。残差が keep_ms を超える拍は本物のテンポ変化とみなして元の値を残す。"""
    n = len(beats)
    if n < 5:
        return beats.copy()
    seg_id = np.concatenate([[0], np.cumsum(gap_mask(beats))])
    out = beats.copy()
    for i in range(n):
        idx = np.arange(max(0, i - half), min(n, i + half + 1))
        idx = idx[seg_id[idx] == seg_id[i]]
        if len(idx) < 4:
            continue
        v = np.polyval(np.polyfit(idx - i, beats[idx], 1), 0)
        if abs(v - beats[i]) * 1000 < keep_ms:
            out[i] = v
    # 平滑化で順序が入れ替わることはないが、念のため単調増加を保証する
    return np.maximum.accumulate(out)


def no_beat_regions(beats: np.ndarray, beat_prob: np.ndarray, duration: float) -> list[tuple[float, float]]:
    """SYNC に使えない区間: 拍が途切れている区間、拍の確からしさが低い拍が続く区間、
    倍/半の補正後もテンポが曲の基準から大きく外れたままの区間（3 連符のノリに引っ張られた 1.5 倍など。Phase 1 P1-3 で確認）。"""
    if len(beats) < 8:
        return [(0.0, float(duration))]
    regions = []
    gaps = gap_mask(beats)
    for i in np.where(gaps)[0]:
        regions.append((float(beats[i]), float(beats[i + 1])))
    fr = np.clip((beats * FPS).round().astype(int), 0, len(beat_prob) - 1)
    for a, b in _runs(beat_prob[fr] < 0.3, 8):
        regions.append((float(beats[a]), float(beats[b - 1])))
    lt = local_tempo(beats)
    ref = np.nanmedian(lt)
    off = np.nan_to_num(np.abs(lt / ref - 1), nan=0) > 0.25
    for a, b in _runs(off, 6):
        regions.append((float(beats[max(0, a - 1)]), float(beats[min(len(beats) - 1, b)])))
    if beats[0] > 3.0:
        regions.append((0.0, float(beats[0])))
    if duration - beats[-1] > 3.0:
        regions.append((float(beats[-1]), float(duration)))
    regions.sort()
    merged: list[list[float]] = []
    for s, e in regions:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in merged]


def raw_metrics(raw_beats: np.ndarray, raw_downs: np.ndarray, beat_prob: np.ndarray) -> dict:
    """Phase 0 の分類(A/B/C)に使った指標。分類の閾値が Phase 0 で決めたものなので、補正前の拍で測る。"""
    fr = np.clip((raw_beats * FPS).round().astype(int), 0, len(beat_prob) - 1)
    conf = float(beat_prob[fr].mean()) if len(fr) else 0.0
    lt = local_tempo(raw_beats)
    valid = lt[np.isfinite(lt)]
    bpm = float(np.median(valid)) if len(valid) else 0.0
    unstable = float(np.mean(np.abs(valid / bpm - 1) > 0.25)) if len(valid) else 1.0
    local = []
    for s in range(0, len(raw_beats) - 16, 16):
        seg = raw_beats[s:s + 16]
        d = np.diff(seg)
        if np.max(d) > 1.75 * np.median(d):
            continue
        ii = np.arange(16)
        local.append(np.sqrt(np.mean((seg - np.polyval(np.polyfit(ii, seg, 1), ii)) ** 2)))
    bi = np.searchsorted(raw_beats, raw_downs - 1e-3)
    lengths = np.diff(bi)
    if len(lengths):
        vals, cnt = np.unique(lengths, return_counts=True)
        dom_ratio = float(cnt.max() / len(lengths))
    else:
        dom_ratio = 0.0
    return {"conf": conf, "unstable": unstable, "local16_ms": float(np.median(local)) * 1000 if local else 999.0,
            "downbeat_consistency": dom_ratio}


def classify(m: dict, no_beat_total: float, drift_pct: float) -> tuple[str, list[str]]:
    """Phase 0 の A/B/C 分類（phase0/classify.py と同じ規則）。"""
    if m["conf"] < 0.9 or m["local16_ms"] > 30 or m["unstable"] > 0.4:
        return "C", ["拍の検出が不安定"]
    reasons = []
    if m["unstable"] > 0.05:
        reasons.append(f"倍/半テンポの取り違え区間 {m['unstable']:.0%}")
    if m["downbeat_consistency"] < 0.85:
        reasons.append(f"小節頭の揺らぎ（一致率 {m['downbeat_consistency']:.0%}）")
    if abs(drift_pct) > 5:
        reasons.append(f"テンポが曲中で {drift_pct:+.0f}% 変化")
    if no_beat_total > 15:
        reasons.append(f"拍なし区間 {no_beat_total:.0f}秒")
    return ("A" if not reasons else "B"), reasons
