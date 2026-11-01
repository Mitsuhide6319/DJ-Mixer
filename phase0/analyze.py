"""Phase 0: 手元の曲で拍・小節頭・テンポ揺れ・拍なし区間・ラウドネスを検証する。

使い方: uv run python phase0/analyze.py [id ...] [--restat]
  --restat: モデルを再実行せず、保存済みの拍時刻から指標とグラフを作り直す
出力: work/phase0/<id>/{result.json, tempo.png, click.mp3}, work/phase0/summary.json
解析済みの曲はスキップする（中断しても再実行で続きから）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
MUSIC = Path(r"G:\MUSIC")
OUT = ROOT / "work" / "phase0"
SR = 44100
FPS = 50  # Beat This! のフレームレート

KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# Camelot: 長調 B / 短調 A
CAMELOT_MAJOR = {0: "8B", 7: "9B", 2: "10B", 9: "11B", 4: "12B", 11: "1B", 6: "2B", 1: "3B", 8: "4B", 3: "5B", 10: "6B", 5: "7B"}
CAMELOT_MINOR = {9: "8A", 4: "9A", 11: "10A", 6: "11A", 1: "12A", 8: "1A", 3: "2A", 10: "3A", 5: "4A", 0: "5A", 7: "6A", 2: "7A"}
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def decode(path: Path) -> np.ndarray:
    """ffmpeg で float32 ステレオ 44.1kHz に展開する。戻り値 shape=(n, 2)。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2).copy()


def estimate_key(y: np.ndarray, sr: int) -> dict:
    import librosa

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=4096).mean(axis=1)
    best = None
    for tonic in range(12):
        for mode, prof in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            r = np.corrcoef(chroma, np.roll(prof, tonic))[0, 1]
            if best is None or r > best[0]:
                best = (r, tonic, mode)
    r, tonic, mode = best
    camelot = (CAMELOT_MAJOR if mode == "major" else CAMELOT_MINOR)[tonic]
    return {"key": f"{KEY_NAMES[tonic]} {mode}", "camelot": camelot, "corr": round(float(r), 3)}


def find_no_beat_regions(beats: np.ndarray, duration: float) -> list[tuple[float, float]]:
    """拍が途切れている区間（ルバート・ブレイク・無音）を返す。"""
    regions = []
    if len(beats) < 8:
        return [(0.0, duration)]
    ibi = np.diff(beats)
    for i, d in enumerate(ibi):
        lo, hi = max(0, i - 8), min(len(ibi), i + 9)
        local = np.median(np.r_[ibi[lo:i], ibi[i + 1:hi]])
        if d > 1.75 * local and d > 1.0:
            regions.append((float(beats[i]), float(beats[i + 1])))
    if beats[0] > 3.0:
        regions.insert(0, (0.0, float(beats[0])))
    if duration - beats[-1] > 3.0:
        regions.append((float(beats[-1]), duration))
    return regions


def local_tempo(beats: np.ndarray, win: int = 8) -> np.ndarray:
    """各拍の周辺 win 拍に直線を当てはめた傾きから局所テンポを求める。
    Beat This! の拍時刻は 20ms(50fps) 刻みなので、隣接拍の差分では BPM が量子化される。"""
    n = len(beats)
    out = np.empty(n)
    h = win // 2
    for i in range(n):
        lo, hi = max(0, i - h), min(n, i + h + 1)
        seg = beats[lo:hi]
        # 拍なし区間をまたぐ窓は、ギャップ手前/後ろ側だけを使う
        d = np.diff(seg)
        med = np.median(d)
        cut = np.where(d > 1.75 * med)[0]
        if len(cut):
            j = i - lo
            left = cut[cut < j]
            right = cut[cut >= j]
            a = left[-1] + 1 if len(left) else 0
            b = right[0] + 1 if len(right) else len(seg)
            seg = seg[a:b]
        if len(seg) < 3:
            out[i] = np.nan
            continue
        slope = np.polyfit(np.arange(len(seg)), seg, 1)[0]
        out[i] = 60.0 / slope
    return out


def tempo_stats(beats: np.ndarray) -> dict:
    curve = local_tempo(beats)
    valid = curve[np.isfinite(curve)]
    bpm = float(np.median(valid))
    n = len(valid)
    head, tail = np.median(valid[: max(1, n // 5)]), np.median(valid[-max(1, n // 5):])
    # 倍/半テンポの取り違え・大きな乱れ: 中央値から ±25% 以上外れた拍の割合
    unstable = float(np.mean(np.abs(valid / bpm - 1) > 0.25))
    # 固定BPMグリッド(曲全体で1本の直線)に当てはめたときのズレ。拍なし区間の前後は別区間として扱う
    ibi = np.diff(beats)
    cuts = np.where(ibi > 1.75 * np.median(ibi))[0]
    resid = []
    for seg in np.split(beats, cuts + 1):
        if len(seg) < 16:
            continue
        idx = np.arange(len(seg))
        resid.append(seg - np.polyval(np.polyfit(idx, seg, 1), idx))
    resid = np.concatenate(resid) if resid else np.zeros(1)
    local = []
    for s in range(0, len(beats) - 16, 16):
        seg = beats[s: s + 16]
        if np.max(np.diff(seg)) > 1.75 * np.median(np.diff(seg)):
            continue
        ii = np.arange(16)
        local.append(np.sqrt(np.mean((seg - np.polyval(np.polyfit(ii, seg, 1), ii)) ** 2)))
    return {
        "bpm_median": round(bpm, 2),
        "bpm_p5": round(float(np.percentile(valid, 5)), 2),
        "bpm_p95": round(float(np.percentile(valid, 95)), 2),
        "drift_pct": round(float((tail - head) / head * 100), 2),
        "unstable_ratio": round(unstable, 3),
        "fixed_grid_rms_ms": round(float(np.sqrt(np.mean(resid**2)) * 1000), 1),
        "fixed_grid_max_ms": round(float(np.max(np.abs(resid)) * 1000), 1),
        "local16_grid_rms_ms": round(float(np.median(local)) * 1000, 1) if local else None,
        "curve_t": beats.round(3).tolist(),
        "curve_bpm": np.nan_to_num(curve, nan=0).round(2).tolist(),
    }


def bar_stats(beats: np.ndarray, downbeats: np.ndarray) -> dict:
    bi = np.searchsorted(beats, downbeats - 1e-3)
    lengths = np.diff(bi)
    hist: dict[str, int] = {}
    for L in lengths:
        hist[str(int(L))] = hist.get(str(int(L)), 0) + 1
    top = max(hist.items(), key=lambda kv: kv[1]) if hist else ("-", 0)
    return {
        "bars": int(len(lengths)),
        "beats_per_bar_hist": dict(sorted(hist.items(), key=lambda kv: int(kv[0]))),
        "dominant_meter": int(top[0]) if hist else None,
        "dominant_ratio": round(top[1] / max(1, len(lengths)), 3),
    }


def make_click(audio: np.ndarray, beats: np.ndarray, downbeats: np.ndarray, gain: float) -> np.ndarray:
    n = len(audio)
    click = np.zeros(n, dtype=np.float32)
    t = np.arange(int(0.04 * SR)) / SR
    env = np.exp(-t * 90)
    tone_beat = (np.sin(2 * np.pi * 1000 * t) * env * 0.35).astype(np.float32)
    tone_down = (np.sin(2 * np.pi * 1760 * t) * env * 0.6).astype(np.float32)
    dset = set(np.round(downbeats, 2))
    for b in beats:
        tone = tone_down if round(float(b), 2) in dset else tone_beat
        s = int(b * SR)
        e = min(n, s + len(tone))
        if s < n:
            click[s:e] += tone[: e - s]
    mix = audio * gain * 0.8 + click[:, None]
    return np.clip(mix, -1, 1)


def plot(res: dict, beats: np.ndarray, downbeats: np.ndarray, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 5), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ts = res["tempo"]
    ax1.plot(ts["curve_t"], ts["curve_bpm"], lw=1.2, color="#1f77b4")
    ax1.axhline(ts["bpm_median"], color="gray", lw=0.8, ls="--")
    for s, e in res["no_beat_regions"]:
        for ax in (ax1, ax2):
            ax.axvspan(s, e, color="#d62728", alpha=0.15)
    ax1.set_ylabel("BPM (8-beat median)")
    ax1.set_title(f"{res['id']}  median {ts['bpm_median']} BPM  drift {ts['drift_pct']}%  "
                  f"fixed-grid RMS {ts['fixed_grid_rms_ms']} ms  meter {res['bars']['dominant_meter']}")
    bi = np.searchsorted(beats, downbeats - 1e-3)
    lengths = np.diff(bi)
    colors = {3: "#2ca02c", 4: "#1f77b4", 5: "#ff7f0e", 6: "#9467bd", 7: "#8c564b"}
    for d0, d1, L in zip(downbeats[:-1], downbeats[1:], lengths):
        ax2.axvspan(d0, d1, color=colors.get(int(L), "#7f7f7f"), alpha=0.6, lw=0)
        ax2.text((d0 + d1) / 2, 0.5, str(int(L)), ha="center", va="center", fontsize=5)
    ax2.set_yticks([])
    ax2.set_ylabel("beats/bar")
    ax2.set_xlabel("time (s)   red = no-beat region")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def analyze(row: dict, model, post) -> dict:
    import librosa
    import pyloudnorm as pyln
    import soundfile as sf

    out_dir = OUT / row["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = decode(MUSIC / row["path"])
    duration = len(audio) / SR
    mono = audio.mean(axis=1)

    beat_logits, down_logits = model(mono, SR)
    beats, downbeats = post(beat_logits, down_logits)
    beats, downbeats = np.asarray(beats), np.asarray(downbeats)
    prob = torch.sigmoid(beat_logits.float()).cpu().numpy()
    fr = np.clip((beats * FPS).round().astype(int), 0, len(prob) - 1)
    conf = prob[fr]

    lufs = pyln.Meter(SR).integrated_loudness(audio.astype(np.float64))
    y22 = librosa.resample(mono, orig_sr=SR, target_sr=22050)
    key_all = estimate_key(y22, 22050)
    key_head = estimate_key(y22[: 22050 * 30], 22050)
    key_tail = estimate_key(y22[-22050 * 30:], 22050)

    res = {
        "id": row["id"], "genre": row["genre"], "note": row["note"], "path": row["path"],
        "duration": round(duration, 2),
        "beats": len(beats), "downbeats": len(downbeats),
        "beat_conf_mean": round(float(conf.mean()), 3),
        "beat_conf_low_ratio": round(float((conf < 0.5).mean()), 3),
        "tempo": tempo_stats(beats),
        "bars": bar_stats(beats, downbeats),
        "no_beat_regions": [(round(s, 2), round(e, 2)) for s, e in find_no_beat_regions(beats, duration)],
        "lufs": round(float(lufs), 2),
        "key": key_all, "key_head30": key_head, "key_tail30": key_tail,
        "beat_times": beats.round(4).tolist(), "downbeat_times": downbeats.round(4).tolist(),
    }
    res["no_beat_total_s"] = round(sum(e - s for s, e in res["no_beat_regions"]), 1)

    plot(res, beats, downbeats, out_dir / "tempo.png")
    gain = 10 ** ((-16 - lufs) / 20) if np.isfinite(lufs) else 1.0  # 確認音声は -16 LUFS 付近に揃える
    wav = out_dir / "click.wav"
    sf.write(wav, make_click(audio, beats, downbeats, gain), SR)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "click.wav", "-b:a", "192k", "click.mp3"],
                   cwd=out_dir, check=True)
    wav.unlink()
    (out_dir / "result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def main() -> None:
    from beat_this.inference import Audio2Frames
    from beat_this.model.postprocessor import Postprocessor

    rows = []
    lines = (ROOT / "phase0" / "tracks.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        rows.append(dict(zip(header, line.split("\t"))))
    restat = "--restat" in sys.argv
    only = {a for a in sys.argv[1:] if not a.startswith("--")}
    model = Audio2Frames(checkpoint_path="final0", device="cuda" if torch.cuda.is_available() else "cpu")
    post = Postprocessor(type="minimal")

    summary = []
    for row in rows:
        if only and row["id"] not in only:
            continue
        rj = OUT / row["id"] / "result.json"
        if rj.exists() and (not only or restat):
            res = json.loads(rj.read_text(encoding="utf-8"))
            if restat:  # 保存済みの拍時刻から指標とグラフだけ作り直す
                b, d = np.array(res["beat_times"]), np.array(res["downbeat_times"])
                res["tempo"], res["bars"] = tempo_stats(b), bar_stats(b, d)
                plot(res, b, d, OUT / row["id"] / "tempo.png")
                rj.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        else:
            print(f"analyze {row['id']} ...", flush=True)
            res = analyze(row, model, post)
        slim = {k: v for k, v in res.items() if k not in ("beat_times", "downbeat_times")}
        slim["tempo"] = {k: v for k, v in res["tempo"].items() if not k.startswith("curve")}
        summary.append(slim)
        t = slim["tempo"]
        print(f"  {res['id']:16s} bpm={t['bpm_median']:6.1f} drift={t['drift_pct']:+5.1f}% "
              f"unst={t['unstable_ratio']:.0%} grid={t['fixed_grid_rms_ms']:7.1f}ms local={t['local16_grid_rms_ms']}ms "
              f"meter={res['bars']['dominant_meter']}({res['bars']['dominant_ratio']:.0%}) "
              f"nobeat={res['no_beat_total_s']}s conf={res['beat_conf_mean']} lufs={res['lufs']} key={res['key']['camelot']}")
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
