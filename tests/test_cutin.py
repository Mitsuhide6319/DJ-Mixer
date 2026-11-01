"""カットイン（design_cutin.md）: 切り替えのタイミング・切り替えポイントの候補・音量合わせ・おまかせのつなぎ方。"""
import numpy as np

from djmix.analysis import cutpoints
from djmix.export import render
from djmix.planner import engine
from tests.test_render import synth_track

ST = {"tempo_range": 0.08, "lufs_target": -14, "xfade_curve": "smooth"}


def test_cut_enters_on_bar_head_with_sub_step_precision():
    # テンポが大きく違う（120 → 90 BPM）。重ねないので拍は合わせず、次の曲は原速のまま 1 拍目から
    a = synth_track(0, 120, 120, t0=10, t1=60.3, join_tpl="cut")
    b = synth_track(1, 90, 150, t0=5, t1=100)
    plan = render.build_plan([a, b], ST)
    assert len(plan.cuts) == 1
    c = plan.cuts[0]
    k, f = c["k"], c["f"]
    ov_end = min(a.bm.bar_times, key=lambda t: abs(t - 60.3))         # 前の曲の使う範囲の終わりに最も近い小節の頭
    in_time = min(b.bm.bar_times, key=lambda t: abs(t - 5))           # 次の曲の入り口の小節の頭
    # 刻み k の f の位置で、前の曲がちょうど小節の頭・次の曲がちょうど入り口（サンプル単位の精度）
    assert abs(plan.pos[0][k] + f * render.DT - ov_end) < 1e-6
    assert abs(plan.pos[1][k] + f * render.DT - in_time) < 1e-6
    assert plan.track[0][k] >= 0 and plan.track[0][k + 1] < 0          # 前の曲は次の刻みで止まる
    rate = np.diff(plan.pos[1][k:k + 200]) / render.DT
    assert np.allclose(rate, 1.0)                                       # 次の曲は原速
    assert abs(plan.marks[1]["t"] - (k + f) * render.DT) < 1e-9


def test_cut_gain_switches_inside_the_step():
    a = synth_track(0, 120, 120, t0=10, t1=60.3, join_tpl="cut")
    b = synth_track(1, 90, 150, t0=5, t1=100)
    plan = render.build_plan([a, b], ST)
    mx = render.Mixdown([a, b], plan, ST)
    c = plan.cuts[0]
    k, i0 = c["k"], int(c["f"] * render.STEP)
    c0 = k - k % mx.CH
    g0, g1 = mx._gain(0, c0, c0 + mx.CH), mx._gain(1, c0, c0 + mx.CH)
    a0 = (k - c0) * render.STEP
    assert np.all(g0[a0:a0 + i0] > 0.99) and np.all(g1[a0:a0 + i0] < 1e-9)          # 切り替えの前は前の曲だけ
    e = a0 + i0 + mx.CUT_FADE + 1
    if e < a0 + render.STEP:
        assert np.all(g0[e:a0 + render.STEP] < 1e-9) and np.all(g1[e:a0 + render.STEP] > 0.99)


def test_cut_trim_auto_manual_and_return():
    bars = lambda db, n=60, bl=2.0: [{"t": i * bl, "db": db} for i in range(n)]          # noqa: E731
    # 前の曲の終わりが −10dB、次の曲の入りが −20dB（LUFS は同じ）→ +6dB で頭打ち
    assert cutpoints.auto_trim_db(bars(-10), 80, 2, -14, bars(-20), 0, 2, -14, -14) == 6.0
    assert cutpoints.auto_trim_db(bars(-10), 80, 2, -14, bars(-13), 0, 2, -14, -14) == 3.0
    assert cutpoints.auto_trim_db(bars(-10), 80, 2, -14, bars(-10.5), 0, 2, -14, -14) == 0.0     # 1dB 未満は合わせない
    # LUFS の揃えも含めて比べる（次の曲は曲全体が 4dB 小さい → 揃えで +4dB 持ち上がるので、差はその分小さい）
    assert cutpoints.auto_trim_db(bars(-10), 80, 2, -14, bars(-13), 0, 2, -18, -14) == -1.0
    assert cutpoints.trim_db({"cutTrim": "off"}, 5.0) == 0.0
    assert cutpoints.trim_db({"cutTrim": -2.5}, 5.0) == -2.5
    assert cutpoints.trim_db({"cutTrim": 20}, 5.0) == 6.0
    assert cutpoints.trim_db({}, 5.0) == 5.0
    # 書き出し: 切り替えの時刻に倍率 g、次の曲の 16 小節で 1 へ戻る
    a = synth_track(0, 120, 120, t0=10, t1=60.3, join_tpl="cut", join_params={"cutTrim": -6})
    b = synth_track(1, 90, 150, t0=5, t1=100)
    plan = render.build_plan([a, b], ST)
    k = plan.cuts[0]["k"]
    g = 10 ** (-6 / 20)
    assert abs(plan.jg[1][k + 1] - g) < 0.01
    back = 16 * 4 * 60 / 90
    k2 = k + int(back / render.DT) + 2
    assert abs(plan.jg[1][k2] - 1) < 1e-9 and plan.jg[0][:k].max() == 1.0


def _cp_analysis(n=48, bl=2.0):
    a = {"bars": [{"beat": 4 * i, "time": i * bl} for i in range(n)], "duration": n * bl, "no_beat": []}
    rms = np.full(n, 0.05)
    rms[16:24] = 0.2                       # 16〜23 小節が盛り上がり（サビ）
    feats = np.zeros((n, 15))
    feats[:, 0] = rms
    segs = [{"bar0": 0}, {"bar0": 16}, {"bar0": 24}, {"bar0": 40}]
    voc = np.zeros((n, 4))
    voc[17:23, 0] = 0.8                    # サビの中だけ歌う
    return a, feats, segs, voc


def test_cutpoint_candidates():
    a, feats, segs, voc = _cp_analysis()
    r = cutpoints.compute(a, feats, segs, voc)
    outs = {c["bar"]: c for c in r["outs"]}
    ins = {c["bar"]: c for c in r["ins"]}
    best = max(r["outs"], key=lambda c: c["score"])
    assert best["bar"] == 24 and "盛り上がりの終わり" in best["tags"] and "区間の切れ目" in best["tags"]     # サビの切れ目
    assert outs[20]["score"] < 0 and "歌の途中" in outs[20]["tags"]
    assert "サビ頭の候補" in ins[16]["tags"] and ins[16]["score"] >= cutpoints.GOOD
    assert "曲の頭" in ins[0]["tags"]
    assert len(r["bars"]) == len(a["bars"])


def test_planner_make_cut_snaps_to_clean_points(monkeypatch):
    a, feats, segs, voc = _cp_analysis(bl=3.0)
    cp = cutpoints.compute(a, feats, segs, voc)
    monkeypatch.setattr(cutpoints, "for_track", lambda tid: cp)
    ta = {"id": 1, "use": {"t0": 0.0, "t1": 78.0, "bar0": 0, "bar1": 26, "notes": []}, "seg": {"pos": "auto"}}
    tb = {"id": 2, "use": {"t0": 42.0, "t1": 135.0, "bar0": 14, "bar1": 45, "notes": []}, "seg": {"pos": "auto"}}
    j = {"tpl": "echo", "bars": 2, "params": {"tempo": "sync", "loop": 0, "shift": 1}, "why": ["拍を合わせられないのでエコーアウト"]}
    assert engine._make_cut(ta, tb, j, forced=False)
    assert j["tpl"] == "cut" and j["bars"] == 0 and j["mode"] == "cut" and j["params"]["shift"] == 0
    assert ta["use"]["bar1"] == 24 and ta["use"]["t1"] == 72.0         # サビの切れ目で切る
    assert tb["use"]["bar0"] == 16                                     # サビ頭から入る
    assert not any(w.startswith("拍を合わせられない") for w in j["why"])
    # 自分で選んだ区間は動かさない
    ta2 = {**ta, "use": {"t0": 0.0, "t1": 78.0, "bar0": 0, "bar1": 26, "notes": []}, "seg": {"pos": "custom"}}
    j2 = {"tpl": "long", "bars": 16, "params": {}, "why": []}
    assert engine._make_cut(ta2, dict(tb), j2, forced=True)
    assert ta2["use"]["bar1"] == 26


def test_planner_join_modes(monkeypatch):
    monkeypatch.setattr(cutpoints, "for_track", lambda tid: None)
    # 切り替えポイントが分からないとき、おまかせはカットインにしない（エコーのまま）。指定されたらカットインにする
    ta = {"id": 1, "use": {"t0": 0.0, "t1": 52.0, "bar0": 0, "bar1": 26}}
    tb = {"id": 2, "use": {"t0": 0.0, "t1": 90.0, "bar0": 0, "bar1": 45}}
    j = {"tpl": "echo", "bars": 2, "params": {}, "why": []}
    assert not engine._make_cut(dict(ta), dict(tb), dict(j), forced=False)
    assert engine._make_cut(dict(ta), dict(tb), j, forced=True) and j["tpl"] == "cut"


def test_reveal_opens_file_or_nearest_folder(tmp_path, monkeypatch):
    """書き出し先を開く（/api/reveal）: ファイルは選んだ状態、無いパスはある所まで親をたどる。実際には起動しない。"""
    from djmix import procutil
    calls = []
    monkeypatch.setattr(procutil.subprocess, "Popen", lambda args, **kw: calls.append(args))
    f = tmp_path / "mix.wav"
    f.write_bytes(b"x")
    assert procutil.reveal(str(f)) == "file" and calls[-1] == ["explorer", f"/select,{f}"]
    assert procutil.reveal(str(tmp_path / "no" / "such")) == "folder" and calls[-1] == ["explorer", str(tmp_path)]
