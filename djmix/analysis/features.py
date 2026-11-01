"""ラウドネス・小節ごとの特徴量・キー・3 帯域波形（design_phase1.md §3.3 の 7〜10）。"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

WAVE_RATE = 200  # 波形点/秒（打合せ7: 100 では拡大表示で階段状に見えた）

KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
CAMELOT_MAJOR = {0: "8B", 7: "9B", 2: "10B", 9: "11B", 4: "12B", 11: "1B", 6: "2B", 1: "3B", 8: "4B", 3: "5B", 10: "6B", 5: "7B"}
CAMELOT_MINOR = {9: "8A", 4: "9A", 11: "10A", 6: "11A", 1: "12A", 8: "1A", 3: "2A", 10: "3A", 5: "4A", 0: "5A", 7: "6A", 2: "7A"}
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# bars.f32 の列（design_phase1.md §3.4）
BAR_COLUMNS = ["rms", "low"] + [f"chroma_{k}" for k in KEY_NAMES] + ["harmonic"]


def loudness(audio: np.ndarray, sr: int) -> tuple[float, float]:
    """(integrated LUFS, サンプルピーク dBFS)。"""
    import pyloudnorm as pyln

    lufs = float(pyln.Meter(sr).integrated_loudness(audio.astype(np.float64)))
    peak = float(20 * np.log10(max(1e-9, np.abs(audio).max())))
    return (lufs if np.isfinite(lufs) else -70.0), peak


def waveform(mono: np.ndarray, sr: int, rate: int = WAVE_RATE) -> np.ndarray:
    """3 帯域（低 <200Hz / 中 200Hz〜2.5kHz / 高 >2.5kHz）のピーク。shape=(3, n) uint8。3 帯域共通の最大値で正規化。"""
    # 44100/200 = 220.5 のように割り切れないので、区切りは小数で計算する
    # （整数の 220 で区切ると 200.45 点/秒になり、10 分の曲で波形が約 1.4 秒ずれる。P1-3 のテストで発見）
    n = int(len(mono) * rate / sr)
    edges = (np.arange(n + 1) * sr / rate).round().astype(np.int64)
    bands = []
    for sos in (butter(4, 200, "lowpass", fs=sr, output="sos"),
                butter(4, [200, 2500], "bandpass", fs=sr, output="sos"),
                butter(4, 2500, "highpass", fs=sr, output="sos")):
        x = np.abs(sosfilt(sos, mono))
        bands.append(np.maximum.reduceat(x[: edges[-1]], edges[:-1]))
    m = max(float(b.max()) for b in bands) or 1.0
    return np.stack([np.clip(b / m * 255, 0, 255) for b in bands]).astype(np.uint8)


def estimate_key(chroma_mean: np.ndarray) -> dict:
    if not np.any(chroma_mean > 0):
        return {"key": None, "camelot": None, "corr": 0.0}
    best = None
    for tonic in range(12):
        for mode, prof in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            r = float(np.corrcoef(chroma_mean, np.roll(prof, tonic))[0, 1])
            if best is None or r > best[0]:
                best = (r, tonic, mode)
    r, tonic, mode = best
    return {"key": f"{KEY_NAMES[tonic]} {mode}", "camelot": (CAMELOT_MAJOR if mode == "major" else CAMELOT_MINOR)[tonic],
            "corr": round(r, 3)}


def bar_features(mono: np.ndarray, sr: int, bar_times: list[float], duration: float) -> tuple[np.ndarray, dict]:
    """小節ごとの特徴量（行=小節、列=BAR_COLUMNS）と、キー（全体・冒頭30秒・末尾30秒）。

    和音の濃さ（harmonic）= クロマの最大値の突出度。ドラムだけの区間では小さくなり、
    Phase 3 の相性判定で「キー衝突の心配がない区間」として扱う（打合せ5）。
    """
    import librosa

    y = librosa.resample(mono, orig_sr=sr, target_sr=22050)
    hop = 2048
    chroma = librosa.feature.chroma_stft(y=y, sr=22050, hop_length=hop, n_fft=4096)
    spec = np.abs(librosa.stft(y, n_fft=4096, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=22050, n_fft=4096)
    low = spec[freqs < 200].sum(axis=0)
    rms = librosa.feature.rms(y=y, frame_length=4096, hop_length=hop)[0]
    t = librosa.frames_to_time(np.arange(chroma.shape[1]), sr=22050, hop_length=hop)

    edges = list(bar_times) + [duration]
    rows = []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (t >= a) & (t < b)
        if not sel.any():
            sel = np.argmin(np.abs(t - a)) == np.arange(len(t))
        c = chroma[:, sel].mean(axis=1)
        harm = float((c.max() - c.mean()) / (c.max() + 1e-9))
        rows.append([float(rms[sel].mean()), float(low[sel].mean())] + [float(v) for v in c] + [harm])
    feats = np.array(rows, dtype=np.float32).reshape(-1, len(BAR_COLUMNS))
    if len(feats):
        # 曲内で相対化（曲どうしの比較はラウドネス正規化後に Phase 3 で行う）
        for col in (0, 1):
            mx = feats[:, col].max()
            if mx > 0:
                feats[:, col] /= mx

    def key_of(mask):
        return estimate_key(chroma[:, mask].mean(axis=1) if mask.any() else np.zeros(12))

    keys = {"all": key_of(np.ones_like(t, dtype=bool)), "head30": key_of(t < 30), "tail30": key_of(t > duration - 30)}
    return feats, keys


def track_extra(mono: np.ndarray, sr: int) -> dict:
    """曲どうしを比べるための曲全体の特徴量（Phase 3。design_phase3.md §3.1）。
    centroid: スペクトルの明るさ(Hz)、low_ratio: 150Hz 未満の割合、onset_rate: 1 秒あたりのオンセット数。"""
    import librosa

    y = librosa.resample(mono, orig_sr=sr, target_sr=11025)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    f = librosa.fft_frequencies(sr=11025, n_fft=2048)
    tot = float(S.sum()) + 1e-9
    onsets = librosa.onset.onset_detect(y=y, sr=11025, hop_length=512, units="time")
    return {"centroid": round(float((S * f[:, None]).sum() / tot), 1),
            "low_ratio": round(float(S[f < 150].sum() / tot), 4),
            "onset_rate": round(len(onsets) / max(1e-3, len(y) / 11025), 3)}
