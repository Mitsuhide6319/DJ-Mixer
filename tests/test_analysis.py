"""P1-3: 解析パイプラインとワーカー。合成クリック音（常に実行）と G:\MUSIC の実曲（あれば実行）で確認する。"""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from djmix.analysis import beats as B
from djmix.analysis.pipeline import analyze_file

MUSIC = Path(r"G:\MUSIC")
need_music = pytest.mark.skipif(not MUSIC.exists(), reason="G:\MUSIC がない環境")


def click_track(path, bpm=120.0, meter=4, seconds=40, sr=44100):
    """1 拍目を強調したクリック＋低音。"""
    n = int(seconds * sr)
    y = np.zeros(n, dtype=np.float32)
    t = np.arange(int(0.05 * sr)) / sr
    beat = 60.0 / bpm
    for k in range(int(seconds / beat)):
        s = int(k * beat * sr)
        down = k % meter == 0
        tone = np.sin(2 * np.pi * (1500 if down else 900) * t) * np.exp(-t * 60) * (0.9 if down else 0.4)
        kick = np.sin(2 * np.pi * 60 * t) * np.exp(-t * 30) * (0.9 if down else 0.3)
        e = min(n, s + len(t))
        y[s:e] += (tone + kick)[: e - s]
    sf.write(path, np.stack([y, y], axis=1), sr)


def test_smooth_removes_quantization_but_keeps_tempo_change():
    rng = np.random.default_rng(0)
    # 20ms の格子に乗らない間隔にする（123 BPM → 136 BPM）
    true = 0.013 + np.cumsum(np.r_[np.full(40, 60 / 123), np.full(40, 60 / 136)])
    q = np.round((true + rng.normal(0, 0.004, len(true))) * 50) / 50   # 20ms 刻みに量子化
    sm = B.smooth(q)
    assert np.sqrt(np.mean((sm - true) ** 2)) < np.sqrt(np.mean((q - true) ** 2))
    lt = B.local_tempo(sm)
    assert abs(np.nanmedian(lt[:30]) - 123) < 1.5 and abs(np.nanmedian(lt[-30:]) - 136) < 1.5


def test_fix_octave_halves_doubled_section():
    beats = np.r_[np.arange(0, 20, 0.5), np.arange(20, 30, 0.25), np.arange(30, 50, 0.5)]
    fixed, n = B.fix_octave(beats, np.ones(3000))
    assert n == 1
    lt = B.local_tempo(fixed)
    assert np.nanmax(np.abs(lt / 120 - 1)) < 0.3


def test_click_track_pipeline(tmp_path):
    p = tmp_path / "click.wav"
    click_track(p, bpm=120, meter=4)
    a, wave, feats, dprob = analyze_file(p)
    assert abs(a["bpm"] - 120) < 1.0
    assert a["meter"] == 4
    assert len(a["bars"]) >= 15 and np.mean([b["conf"] for b in a["bars"]]) > 0.5
    # 小節頭は 4 拍（2 秒）おきに一定の位相で並ぶ。どの拍を 1 拍目とみなすかはモデル次第
    # （合成クリックでは Beat This! が 2 拍目を小節頭と判定する。実曲の 1 拍目はユーザーが P1-7 で直せる）
    t = np.array([b["time"] for b in a["bars"]])
    assert np.allclose(np.diff(t), 2.0, atol=0.03)
    assert wave.shape[0] == 3 and wave.dtype == np.uint8 and abs(wave.shape[1] - 40 * 200) < 5
    assert feats.shape == (len(a["bars"]), len(a["bar_columns"]))
    # 曲全体の特徴量（Phase 3 のおまかせ用）
    assert a["extra"]["onset_rate"] > 1 and a["extra"]["centroid"] > 0 and 0 <= a["extra"]["low_ratio"] <= 1


@need_music
@pytest.mark.parametrize("path,bpm,meter,cls", [
    ("Daft Punk/Human After All/09 Technologic.m4a", 127.5, 4, "A"),
    ("Miles Davis - Kind of Blue/04. All Blues.mp3", 139.4, 3, "A"),
])
def test_real_tracks(path, bpm, meter, cls):
    a, *_ = analyze_file(MUSIC / path)
    assert abs(a["bpm"] - bpm) < 1.0 and a["meter"] == meter and a["beat_class"] == cls


@need_music
def test_fourplay_triplet_section_is_marked_unusable():
    """Phase 1 P1-3 で見つけた 1.5 倍区間（160〜167 秒付近）は SYNC に使わない区間になる。"""
    a, *_ = analyze_file(MUSIC / "Fourplay - 1993 - Between the Sheets/01 - Chant.mp3")
    assert a["octave_fixes"] >= 1
    assert any(s < 163 < e for s, e in a["no_beat"])
    assert abs(a["bpm"] - 89.0) < 1.0
