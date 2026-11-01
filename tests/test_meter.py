"""拍子が途中で変わる曲の小節頭（拍単位の Viterbi。bars.track_meter / bar_phases_varying）。合成した小節頭らしさで確かめる。"""
import numpy as np

from djmix.analysis import bars


def strength_of(meters, noise=0.12, seed=0, hi=0.7, lo=0.15):
    """小節ごとの拍子の列 meters から、各拍の小節頭らしさ（小節頭 hi、それ以外 lo、正規ノイズ）を作る。"""
    rng = np.random.default_rng(seed)
    s = np.concatenate([[hi] + [lo] * (m - 1) for m in meters])
    return np.clip(s + rng.normal(0, noise, len(s)), 0, 1)


def heads(path):
    return [k for k, (_, b) in enumerate(path) if b == 1]


def test_money_style_switch():
    ms = [7] * 12 + [4] * 16
    path = bars.track_meter(strength_of(ms))
    want = np.cumsum([0] + ms[:-1]).tolist()
    assert heads(path) == want
    assert path[0][0] == 7 and path[-1][0] == 4


def test_single_inserted_bar_is_found_most_of_the_time():
    ms = [4] * 12 + [3] + [4] * 12
    ok = sum(heads(bars.track_meter(strength_of(ms, seed=s))) == np.cumsum([0] + ms[:-1]).tolist() for s in range(40))
    assert ok >= 34                                   # 合成データの調査では 93〜98%


def test_plain_four_four_has_no_switch():
    for s in range(20):
        path = bars.track_meter(strength_of([4] * 40, noise=0.18, seed=s))
        assert {m for m, _ in path} == {4}


def test_three_four_stays_three_not_six():
    path = bars.track_meter(strength_of([3] * 32, seed=3))
    assert {m for m, _ in path} == {3}


def test_bar_phases_varying_and_summary():
    ms = [7] * 6 + [4] * 10
    s = strength_of(ms, noise=0.05)
    beats = np.arange(len(s)) * 0.5 + 1.0
    bl = bars.bar_phases_varying(beats, s)
    assert [b["meter"] for b in bl] == ms
    assert bl[6]["beat"] == 42 and abs(bl[6]["time"] - 22.0) < 1e-9
    assert all(0 <= b["conf"] <= 1 for b in bl) and min(b["conf"] for b in bl) > 0.5
    sm = bars.meter_summary(bl)
    assert sm == {"meter": 4, "switches": 1, "counts": {4: 10, 7: 6}}


def test_gap_splits_segments():
    s = strength_of([4] * 10, noise=0.03)
    beats = np.arange(len(s)) * 0.5
    beats[20:] += 10.0                               # 拍の途切れ（10 秒）
    bl = bars.bar_phases_varying(beats, s)
    assert [b["beat"] for b in bl] == list(range(0, 40, 4))
