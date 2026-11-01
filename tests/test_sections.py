"""P2-1: 区間の候補（曲の構造の区切り）。"""
from pathlib import Path

import numpy as np
import pytest

from djmix.analysis.features import BAR_COLUMNS
from djmix.analysis.sections import compute

MUSIC = Path(r"G:\MUSIC")


def synthetic(n=120, changes=(40, 80), bar_sec=2.0):
    rng = np.random.default_rng(1)
    feats = np.zeros((n, len(BAR_COLUMNS)), dtype=np.float32)
    level = np.ones(n) * 0.3
    level[changes[0]:changes[1]] = 0.9          # 真ん中だけ盛り上がる
    feats[:, 0] = level + rng.normal(0, 0.02, n)
    feats[:, 1] = level * 0.8 + rng.normal(0, 0.02, n)
    feats[:, 2:14] = rng.random((n, 12)) * 0.1 + 0.5
    feats[:, -1] = 0.5
    a = {"bars": [{"beat": i * 4, "time": i * bar_sec} for i in range(n)], "duration": n * bar_sec, "no_beat": []}
    return a, feats


def test_finds_the_energy_changes():
    a, feats = synthetic()
    segs = compute(a, feats)
    cuts = [s["bar0"] for s in segs[1:]]
    assert any(abs(c - 40) <= 2 for c in cuts) and any(abs(c - 80) <= 2 for c in cuts), cuts
    mid = next(s for s in segs if s["bar0"] <= 60 < s["bar1"])
    assert "盛り上がり" in mid["tags"]
    assert segs[0]["tags"][0] == "イントロ候補" and segs[-1]["tags"][0] == "アウトロ候補"


def test_sections_are_not_too_small_nor_too_many():
    a, feats = synthetic(n=300, changes=(50, 250))
    segs = compute(a, feats)
    assert len(segs) <= max(4, int(a["duration"] / 35))
    assert all(s["bars"] >= 12 or (s["t1"] - s["t0"]) >= 20 for s in segs)
    assert segs[0]["t0"] == 0 and abs(segs[-1]["t1"] - a["duration"]) < 1e-6
    assert all(x["t1"] == y["t0"] for x, y in zip(segs, segs[1:]))    # 隙間なく並ぶ


@pytest.mark.skipif(not MUSIC.exists(), reason="G:\MUSIC がない環境")
def test_so_what_intro_boundary():
    """So What のテンポが自由なイントロ（〜32 秒）の終わりが区切りになる（Phase 2 企画の試作で確認した結果の再現）。"""
    from djmix.analysis.pipeline import analyze_file
    a, _wave, feats, _dp = analyze_file(MUSIC / "Miles Davis - Kind of Blue/01. So What.mp3")
    segs = compute(a, feats)
    assert any(abs(s["t0"] - 32) < 3 for s in segs[1:]), [s["t0"] for s in segs]
    assert "拍なし" in segs[0]["tags"]
