"""P4-6: 音源分離で小節ごとのパートの量（design_phase4.md §3.1）。合成のクリック曲で試す（GPU があれば数秒）。"""
import numpy as np

from djmix.analysis import stems
from djmix.audio.decode import SR, decode
from djmix.planner import desc
from tests.test_analysis import click_track
from tests.test_planner import synth


def test_compute_shape_and_click_has_no_vocals(tmp_path):
    p = tmp_path / "c.wav"
    click_track(p, bpm=120, seconds=20)
    bars = [i * 2.0 for i in range(10)]
    arr = stems.compute(decode(p), SR, bars, 20.0)
    assert arr.shape == (10, 4) and arr.max() == 1.0
    assert arr[:, stems.SOURCES.index("vocals")].mean() < arr[:, stems.SOURCES.index("drums")].mean()


def test_desc_has_vocals_in_use_range():
    a, feats = synth(n=100)
    st = np.zeros((100, 4), dtype=np.float32)
    st[50:, 0] = 0.8                                           # 後半だけ歌がある
    d = desc.build(a, feats, st)
    u = d["use"]
    assert len(d["vocals"]) == u["bar1"] - u["bar0"] and d["vocals"][-1] == 0.8 and d["vocals"][0] == 0.0
    assert desc.build(a, feats)["vocals"] is None
