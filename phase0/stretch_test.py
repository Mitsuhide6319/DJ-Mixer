"""Phase 0-5: Signalsmith Stretch の音質と「テンポが揺れる曲を一定グリッドに追従させる」精度の検証。

出力: work/phase0/stretch/*.mp3, stretch_result.json
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import numpy as np
import python_stretch as ps
import soundfile as sf
import torch

from analyze import MUSIC, OUT, SR, decode, local_tempo

DST = OUT / "stretch"
BLOCK = 1024


def to_mp3(audio: np.ndarray, name: str) -> None:
    wav = DST / f"{name}.wav"
    sf.write(wav, np.clip(audio, -1, 1), SR)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav.name, "-b:a", "192k", f"{name}.mp3"], cwd=DST, check=True)
    wav.unlink()


def new_stretch() -> ps.Signalsmith.Stretch:
    s = ps.Signalsmith.Stretch()
    s.preset(2, SR)
    return s


def stretch_mapped(audio: np.ndarray, in_of_out) -> np.ndarray:
    """出力時刻 → 入力時刻 の写像 in_of_out(t_out_sec) に従ってブロック単位で可変ストレッチする。
    毎ブロック『目標の入力位置 − 現在の入力位置』から読み込み量を決めるので誤差が蓄積しない。"""
    s = new_stretch()
    x = audio.T.astype(np.float32).copy()  # (2, n)
    out, ip, op = [], 0, 0
    n_in = x.shape[1]
    while ip < n_in:
        target = int(round(in_of_out((op + BLOCK) / SR) * SR))
        n = max(1, min(target - ip, n_in - ip))
        s.setTimeFactor(n / BLOCK)
        y = s.process(x[:, ip: ip + n])
        out.append(y)
        ip += n
        op += y.shape[1]
    # python-stretch はレイテンシ補正済みの出力を返す（インパルス試験で確認済み）ので詰めない
    return np.concatenate(out, axis=1).T


def detect(audio: np.ndarray, model) -> tuple[np.ndarray, np.ndarray]:
    from beat_this.model.postprocessor import Postprocessor

    bl, dl = model(audio.mean(axis=1), SR)
    b, d = Postprocessor(type="minimal")(bl, dl)
    return np.asarray(b), np.asarray(d)


def click_track(times: np.ndarray, n: int, freq: float = 1500) -> np.ndarray:
    c = np.zeros(n, dtype=np.float32)
    t = np.arange(int(0.03 * SR)) / SR
    tone = (np.sin(2 * np.pi * freq * t) * np.exp(-t * 120) * 0.5).astype(np.float32)
    for tt in times:
        s = int(tt * SR)
        if 0 <= s < n:
            e = min(n, s + len(tone))
            c[s:e] += tone[: e - s]
    return c


def main() -> None:
    from beat_this.inference import Audio2Frames

    DST.mkdir(parents=True, exist_ok=True)
    model = Audio2Frames(checkpoint_path="final0", device="cuda" if torch.cuda.is_available() else "cpu")
    report: dict = {}

    # --- 1) 一定倍率の音質: ジャジーハウスとジャズで -5〜-17% ---
    cases = [
        ("hs_tomahawk", "Rasmus Faber/はるかなレシーブ オリジナルサウンドトラック [Disc 1]/1-02 Tomahawk.wav", 30, [1.0, 0.917, 0.834]),
        ("jz_sowhat", "Miles Davis - Kind of Blue/01. So What.mp3", 90, [1.0, 0.945, 0.88]),
    ]
    for cid, path, start, factors in cases:
        a = decode(MUSIC / path)[start * SR: (start + 30) * SR]
        for f in factors:
            s = new_stretch()
            s.setTimeFactor(f)
            t0 = time.perf_counter()
            y = s.process(a.T.astype(np.float32).copy()).T
            dt = time.perf_counter() - t0
            name = f"const_{cid}_{int(round(f * 1000))}"
            to_mp3(y, name)
            report[name] = {"factor": f, "realtime_x": round((len(a) / SR) / dt, 1)}
            print(name, report[name])

    # --- 2) テンポが揺れる Blue Train を 140 BPM 固定グリッドに追従させる ---
    full = json.loads((OUT / "jz_bluetrain" / "result.json").read_text(encoding="utf-8"))
    beats = np.array(full["beat_times"])
    sel = beats[(beats >= 60) & (beats <= 300)]  # 倍テンポ誤検出区間(445-475s)を避けた範囲
    curve = local_tempo(sel)
    bpm_target = 140.0
    grid = np.arange(len(sel)) * 60 / bpm_target  # 出力側の拍時刻
    audio = decode(MUSIC / full["path"])

    def in_of_out(t: float) -> float:
        # 拍どうしを線形補間。範囲外は端の区間の傾きで外挿
        if t <= grid[-1]:
            return float(np.interp(t, grid, sel))
        slope = (sel[-1] - sel[-2]) / (grid[-1] - grid[-2])
        return float(sel[-1] + (t - grid[-1]) * slope)

    seg = audio[int(sel[0] * SR): int(sel[-1] * SR) + SR]
    t0 = time.perf_counter()
    y = stretch_mapped(seg, lambda t: in_of_out(t) - sel[0])
    dt = time.perf_counter() - t0
    n = min(len(y), int(grid[-1] * SR) + SR // 2)
    y = y[:n]

    # 検証: 出力を再度拍検出し、140BPM グリッドとのズレを測る
    ob, _ = detect(y, model)
    def signed_err(det):
        return np.array([b - grid[np.argmin(np.abs(grid - b))] for b in det if b < grid[-1]])

    e = signed_err(ob)
    near = np.abs(e)
    report["follow_bluetrain"] = {
        "offbeat_ratio_50ms": round(float(np.mean(np.abs(e - np.median(e)) > 0.05)), 3),
        "offset_median_ms": round(float(np.median(e)) * 1000, 1),
        "jitter_p95_ms": round(float(np.percentile(np.abs(e - np.median(e)), 95)) * 1000, 1),
        "input_bpm_range": [round(float(np.nanpercentile(curve, 5)), 1), round(float(np.nanpercentile(curve, 95)), 1)],
        "target_bpm": bpm_target,
        "beats": int(len(sel)),
        "err_median_ms": round(float(np.median(near)) * 1000, 1),
        "err_p95_ms": round(float(np.percentile(near, 95)) * 1000, 1),
        "realtime_x": round((n / SR) / dt, 1),
    }
    print("follow", report["follow_bluetrain"])
    metro = click_track(grid, n)
    to_mp3(y * 0.8 + metro[:, None], "follow_bluetrain_140_click")
    # 比較用: 原曲を固定倍率(中央値テンポ→140)で伸縮しただけのもの（DJソフトの固定グリッド相当）
    f = float(np.nanmedian(curve)) / bpm_target
    s = new_stretch()
    s.setTimeFactor(f)
    yc = s.process(seg.T.astype(np.float32).copy()).T[:n]
    ob2, _ = detect(yc, model)
    e2 = signed_err(ob2)
    # 固定倍率では拍がグリッドから徐々にずれていく。半拍(214ms)以上ずれると隣の拍に吸着するので、
    # 同期が崩れた割合として「中央値オフセットから 50ms 以上外れた拍の割合」を見る
    near2 = np.abs(e2 - np.median(e2))
    report["fixed_ratio_bluetrain"] = {
        "offbeat_ratio_50ms": round(float(np.mean(near2 > 0.05)), 3),
        "err_median_ms": round(float(np.median(near2)) * 1000, 1),
        "err_p95_ms": round(float(np.percentile(near2, 95)) * 1000, 1),
    }
    print("fixed", report["fixed_ratio_bluetrain"])
    to_mp3(yc * 0.8 + click_track(grid + (ob2[0] - grid[0]), len(yc))[:, None], "fixed_bluetrain_140_click")
    (DST / "stretch_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
