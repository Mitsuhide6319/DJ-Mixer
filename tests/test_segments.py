"""使ってみての改善 I-7: 1 曲あたりの長さ・使う区間（planner/segments.py）。合成の解析キャッシュで試す。"""
import numpy as np
import pytest

from djmix.analysis import cache
from djmix.analysis.features import BAR_COLUMNS
from djmix.planner import segments


@pytest.fixture()
def track(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    segments._memo.clear()
    n = 200                                                   # 2 秒の小節 × 200 = 400 秒
    a = {"bars": [{"beat": 4 * i, "time": 2.0 * i} for i in range(n)], "duration": 400.0, "no_beat": [], "meter": 4, "bpm": 120.0}
    rng = np.random.default_rng(1)
    feats = rng.uniform(0.2, 0.3, (n, len(BAR_COLUMNS))).astype(np.float32)
    feats[100:140, 0] = 0.9                                   # 200〜280 秒が一番大きい（盛り上がり）
    cache.write(7, a, np.zeros((3, 10), np.uint8), feats, np.zeros(10, np.float16))
    base = {"use": {"t0": 0.0, "t1": 330.0, "bar0": 0, "bar1": 165, "notes": []}}
    return base


def test_variants(track):
    head = segments.variant(7, track, 180, "head")["use"]
    assert head["t0"] == 0 and 150 <= head["t1"] <= 210
    tail = segments.variant(7, track, 180, "tail")["use"]
    assert tail["t1"] == 400 and 150 <= tail["t1"] - tail["t0"] <= 210
    peak = segments.variant(7, track, 120, "peak")["use"]
    assert peak["t0"] <= 220 and peak["t1"] >= 260                           # 盛り上がり（200〜280 秒）を含む
    full = segments.variant(7, track, None, "full")["use"]
    assert (full["t0"], full["t1"]) == (0, 400)
    auto = segments.variant(7, track, 240, "auto")["use"]
    assert auto["t0"] == 0 and 200 <= auto["t1"] <= 280
    assert segments.variant(7, track, None, "auto") is None                  # 指定なし → 要約の範囲のまま
    v = segments.variant(7, track, 180, "head")
    assert {"in", "out", "seg"} <= set(v) and v["seg"] == {"len": 180, "pos": "head"}


def test_choices(track):
    c = segments.choices(7, track, 180)
    assert c[0]["pos"] == "auto" and {"peak", "tail", "full"} <= {x["pos"] for x in c}   # 頭から はおまかせと同じ範囲なのでまとめられる
    assert len({(x["use"]["bar0"], x["use"]["bar1"]) for x in c}) == len(c)  # 同じ範囲はまとめる


@pytest.fixture()
def late_start(tmp_path, monkeypatch):
    """最初の小節頭が 1.5 秒（CONTROL のように、冒頭に小節の前の音がある曲）。"""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    segments._memo.clear()
    n = 199
    a = {"bars": [{"beat": 4 * i, "time": 1.5 + 2.0 * i} for i in range(n)], "duration": 400.0, "no_beat": [], "meter": 4, "bpm": 120.0}
    feats = np.random.default_rng(2).uniform(0.2, 0.3, (n, len(BAR_COLUMNS))).astype(np.float32)
    cache.write(8, a, np.zeros((3, 10), np.uint8), feats, np.zeros(10, np.float16))
    return {"use": {"t0": 1.5, "t1": 331.5, "bar0": 0, "bar1": 165, "notes": []}}


def test_intro_full_custom_start_at_zero(late_start):
    intro = segments.variant(8, late_start, 180, "intro")["use"]
    assert intro["t0"] == 0.0 and 150 <= intro["t1"] <= 210                  # 冒頭（0:00）から
    full = segments.variant(8, late_start, None, "full")["use"]
    assert (full["t0"], full["t1"]) == (0.0, 400.0)                           # 曲全体は 0:00〜最後
    cu = segments.variant(8, late_start, None, "custom", 0.0, 120.4)
    assert cu["use"]["t0"] == 0.0 and abs(cu["use"]["t1"] - 119.5) < 0.01 and cu["seg"]["pos"] == "custom"   # 一番近い小節頭
    cu2 = segments.variant(8, late_start, None, "custom", 60.2, 400)["use"]
    assert abs(cu2["t0"] - 59.5) < 0.01 and cu2["t1"] == 400.0               # 小節にそろえる・最後はそのまま
    assert segments.variant(8, late_start, None, "custom", 50, 60) is None    # 短すぎる区間は使わない
    labels = [c["label"] for c in segments.choices(8, late_start, 180)]
    assert "冒頭（0:00）から" in labels and "曲全体（0:00〜最後）" in labels


def test_short_lengths_use_the_peak(track):
    """短くたくさん繋ぐ（2026-10-25）: 30〜90 秒は「おまかせ」でも盛り上がりの所を使い、長さに近い範囲になる。"""
    for L in (30, 45, 60, 90):
        u = segments.variant(7, track, L, "auto")["use"]
        assert 0.6 * L <= u["t1"] - u["t0"] <= 1.4 * L, (L, u)
        assert 196 <= u["t0"] and u["t1"] <= 284, (L, u)                     # 盛り上がり（200〜280 秒）の中
        assert u["notes"][0].startswith("盛り上がり中心に約") and "秒" in u["notes"][0]
    assert segments.variant(7, track, 30, "head")["use"]["t0"] == 0
