"""P3-8: 高速書き出し（design_phase3.md §6.2）。一定テンポの合成の曲で、拍の写像・原速へ戻す・リミッターを確かめる。"""
import numpy as np
import soundfile as sf

from djmix.export import render
from djmix.export import transition as T
from tests.test_analysis import click_track


def synth_track(idx, bpm, seconds, path=None, t0=0.0, t1=None, **item):
    beat = 60 / bpm
    beats = [0.25 + i * beat for i in range(int((seconds - 0.5) / beat))]
    a = {"beats": beats, "meter": 4, "bars": [{"beat": i, "time": b} for i, b in enumerate(beats) if i % 4 == 0],
         "no_beat": [], "duration": seconds, "lufs": -14.0}
    it = {"track_id": idx + 1, "path": str(path) if path else "", **item}
    return render.Track(idx, it, a, render.BeatMap(a), t0, t1 if t1 is not None else seconds - 1, -14.0, f"t{idx}", "")


def test_plan_locks_beats_and_returns_to_original_speed():
    a = synth_track(0, 120, 120, t0=10, t1=100, join_tpl="long", join_bars=16)
    b = synth_track(1, 126, 150, t0=5, t1=140)
    plan = render.build_plan([a, b], {"tempo_range": 0.08, "tempo_return": True})
    both = [s for s in range(plan.n) if plan.track[0][s] >= 0 and plan.track[1][s] >= 0]
    assert both, "重なりが無い"
    # 重なりの間、次の曲の拍位置 − 前の曲の拍位置 が一定の整数（位相がそろっている）
    d = [b.bm.pos(plan.pos[1][s]) - a.bm.pos(plan.pos[0][s]) for s in both]
    assert np.ptp(d) < 1e-6 and abs(d[0] - round(d[0])) < 1e-6
    # 重なりの長さは 16 小節（前の曲の拍で 64 拍）
    assert abs(a.bm.pos(plan.pos[0][both[-1]]) - a.bm.pos(plan.pos[0][both[0]]) - 64) < 0.2
    # 繋いだ後、次の曲の速度は 120/126 から 8 小節かけて 1 に戻り、範囲の終わりで止まる
    solo = [s for s in range(both[-1] + 1, plan.n) if plan.track[1][s] >= 0]
    rate = np.diff(plan.pos[1][solo]) / render.DT
    assert abs(rate[0] - 120 / 126) < 0.01 and abs(rate[-1] - 1) < 1e-6
    assert abs(plan.pos[1][plan.n - 1] - 140) < 0.05
    assert [m["title"] for m in plan.marks] == ["t0", "t1"]


def test_echo_join_enters_at_end():
    a = synth_track(0, 120, 120, t0=10, t1=100, join_tpl="echo")
    b = synth_track(1, 120, 150, t0=5, t1=60)
    plan = render.build_plan([a, b], {"tempo_range": 0.08})
    first_b = next(s for s in range(plan.n) if plan.track[1][s] >= 0)
    last_a = max(s for s in range(plan.n) if plan.track[0][s] >= 0)
    assert abs(first_b - last_a) <= 2                           # 前の曲の終わりで入る
    assert plan.echo[0].max() > 0.9                              # 最後の小節でエコーを上げている


def test_mixdown_length_and_ceiling(tmp_path):
    pa, pb = tmp_path / "a.wav", tmp_path / "b.wav"
    click_track(pa, bpm=120, seconds=40)
    click_track(pb, bpm=124, seconds=40)
    a = synth_track(0, 120, 40, pa, t0=2, t1=30, join_tpl="short", join_bars=8)
    b = synth_track(1, 124, 40, pb, t0=1, t1=30)
    for t in (a, b):
        t.lufs = -30.0                                           # 大きく持ち上げさせて、リミッターを働かせる
    st = {"tempo_range": 0.08, "lufs_target": -14, "xfade_curve": "smooth"}
    plan = render.build_plan([a, b], st)
    out = []
    for _ in render.Mixdown([a, b], plan, st).run(out.append):
        pass
    y = np.frombuffer(b"".join(out), "<i2").reshape(-1, 2) / 32767
    assert abs(len(y) - plan.n * render.STEP) <= render.STEP
    assert np.abs(y).max() <= 10 ** (-0.3 / 20) + 1e-3          # 0dBFS を超えない（−0.3dBFS で止める）
    assert np.abs(y).max() > 0.5


def test_levels_match_fixture():
    import json
    fx = json.load(open("frontend/tests/levels_fixture.json", encoding="utf-8"))
    for c in fx["cases"][:50]:
        assert T.levels(T.join_of(c["item"]), c["u"], c["beats"]) == c["out"]


def test_plan_ramp_loop_shift():
    a = synth_track(0, 120, 120, t0=10, t1=100, join_tpl="long", join_bars=16,
                    join_params={"tempo": "ramp", "loop": 4, "shift": 1})
    b = synth_track(1, 126, 150, t0=5, t1=140)
    plan = render.build_plan([a, b], {"tempo_range": 0.08, "tempo_return": True})
    both = [s for s in range(plan.n) if plan.track[0][s] >= 0 and plan.track[1][s] >= 0]
    pa = plan.pos[0][both]
    assert (np.diff(pa) < -1).sum() == 1                          # ループで 1 回巻き戻る（最後の 4 小節を繰り返す）
    rb = np.diff(plan.pos[1][both]) / render.DT
    assert abs(rb[-1] - 1) < 0.005 and rb[0] < 0.97               # テンポランプ: 次の曲は重なりの終わりで原速
    # 次の曲の拍の進みは、前の曲の 16 小節（64 拍）ぶん
    assert abs(b.bm.pos(plan.pos[1][both[-1]]) - b.bm.pos(plan.pos[1][both[0]]) - 64) < 0.5
    assert b.shift == 1                                            # キー合わせ
    solo = [s for s in range(both[-1] + 1, plan.n) if plan.track[1][s] >= 0]
    assert abs(np.diff(plan.pos[1][solo[:50]]).mean() / render.DT - 1) < 0.002   # 繋いだ後に原速へ戻す動きは無い


# ---- パートごとに鳴らす（Phase 5 B2） ----
def test_part_gains():
    j = T.join_of({"join_tpl": "long", "join_params": {"parts": "swap"}})
    g0, g1 = T.part_gains(j, 0, 64), T.part_gains(j, 1, 64)
    assert g0["out"] == [1, 1, 1, 1] and g0["in"] == [0, 1, 0, 0]          # 前の曲はそのまま、次の曲はドラムだけ
    assert g1["in"] == [1, 1, 1, 1] and g1["out"][0] == 0 and g1["out"][2] == 0
    assert abs(T.part_gains(j, 0.125, 64)["out"][0] - 0.5) < 1e-9          # 歌は最初の 1/4 で消す
    assert T.levels(j, 0.9, 64)["lowIn"] == 1 and T.levels(j, 0.1, 64)["lowOut"] == 1   # EQ の LOW は動かさない
    assert T.part_gains(T.join_of({"join_tpl": "echo", "join_params": {"parts": "swap"}}), 0.5) is None
    assert T.part_gains(T.join_of({"join_tpl": "long"}), 0.5) is None


def test_mixdown_with_parts(tmp_path, monkeypatch):
    """分離した音の代わりに「元の音の 1/4 × 4 パート」を置くと、パートの合計 = 元の音。
    パートを分けない繋ぎと比べて、重なりの外は同じ、重なりの前半は次の曲が小さい（ドラムだけ）こと。"""
    from djmix.analysis import cache, stems
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    pa, pb = tmp_path / "a.wav", tmp_path / "b.wav"
    click_track(pa, bpm=120, seconds=40)
    click_track(pb, bpm=120, seconds=40)
    for tid, p in ((1, pa), (2, pb)):
        x, sr = sf.read(p, dtype="float32", always_2d=True)
        cache.track_dir(tid).mkdir(parents=True)
        stems.save_audio(tid, np.stack([x.T * 0.25] * 4), sr)
        assert stems.audio_ready(tid)
    st = {"tempo_range": 0.08, "lufs_target": -14, "xfade_curve": "smooth"}

    def mix(parts):
        a = synth_track(0, 120, 40, pa, t0=2, t1=30, join_tpl="long", join_bars=8, join_params={"parts": parts})
        b = synth_track(1, 120, 40, pb, t0=1, t1=30)
        a.parts_ok = b.parts_ok = True
        plan = render.build_plan([a, b], st)
        out = []
        for _ in render.Mixdown([a, b], plan, st, split=True).run(out.append):
            pass
        return plan, np.frombuffer(b"".join(out), "<i2").reshape(-1, 2) / 32767

    p0, y0 = mix("none")
    p1, y1 = mix("drums")
    both = [s for s in range(p1.n) if p1.track[0][s] >= 0 and p1.track[1][s] >= 0]
    assert p1.pm[1][both[0]] == 1 and p1.pm[1][both[-1] + 2] == 0 and p0.pm[1].max() == 0
    S = render.STEP
    before = slice(0, both[0] * S - S * 200)
    assert np.allclose(y0[before], y1[before], atol=1e-3)                    # 重なりの前は同じ
    early = slice(both[0] * S + S * 20, both[0] * S + (both[-1] - both[0]) * S // 5)
    after = slice((both[-1] + 300) * S, min(len(y0), len(y1)) - S)
    # 比べるのは同じ曲の重なりの後（全パート・EQ もそのまま）。「パートを分けない」側は重なりの前半に EQ で低音を切るので基準にしない
    r = np.sqrt(np.mean(y1[early, 1] ** 2)) / np.sqrt(np.mean(y1[after, 1] ** 2))
    assert 0.2 < r < 0.3                                                    # 次の曲はドラムだけ（1/4）
    assert np.corrcoef(y0[after, 1], y1[after, 1])[0, 1] > 0.98            # 重なりの後は元の音に戻る


def test_tracklist_text_has_album():
    from djmix.export.writer import tracklist_text
    t = tracklist_text([{"t": 0, "title": "CONTROL", "artist": "T-SQUARE", "album": "Wave"}, {"t": 245.4, "title": "X", "artist": ""},
                        {"t": 3725, "title": "Y", "artist": "Z", "album": None}])
    assert t.splitlines() == ["00:00  CONTROL - T-SQUARE 『Wave』", "04:05  X", "1:02:05  Y - Z"]


# ---- 画面のストレッチで計算した音を使う書き出し（2026-10-02） ----
def test_segments_and_external_audio(tmp_path):
    """区間はデッキごと・曲ごとに切れ目なく（重ならず）並び、受け取った音は刻みどおりの位置に入る。python-stretch の区切りの落ち込みが出ないこと。"""
    pa, pb = tmp_path / "a.wav", tmp_path / "b.wav"
    click_track(pa, bpm=120, seconds=40)
    click_track(pb, bpm=124, seconds=40)
    a = synth_track(0, 120, 40, pa, t0=2, t1=30, join_tpl="short", join_bars=8)
    b = synth_track(1, 124, 40, pb, t0=1, t1=30)
    st = {"tempo_range": 0.08, "lufs_target": -14, "xfade_curve": "smooth"}
    plan = render.build_plan([a, b], st)
    segs = render.segments_of(plan, [a, b])
    assert [(g["deck"], g["kind"]) for g in segs] == [(0, "main"), (1, "main")]
    for g in segs:                                            # 区間の中はずっと同じ曲
        assert (plan.track[g["deck"]][g["s0"]:g["s1"]] == g["idx"]).all()
        assert g["s0"] == 0 or plan.track[g["deck"]][g["s0"] - 1] != g["idx"]
    # 画面から届いた音の代わりに、一定の大きさの音（L=0.1, R=-0.1）を入れて、ミキサーの手前で取り出す
    ext = {g["id"]: {**g, "audio": np.stack([np.full((g["s1"] - g["s0"]) * render.STEP, 0.1, np.float32),
                                             np.full((g["s1"] - g["s0"]) * render.STEP, -0.1, np.float32)])} for g in segs}
    m = render.Mixdown([a, b], plan, st, ext=ext)
    x = m._deck_audio(1, 0, plan.n)
    on = np.repeat(plan.track[1] >= 0, render.STEP)
    assert np.allclose(x[on, 0], 0.1) and np.allclose(x[on, 1], -0.1) and np.allclose(x[~on], 0)


def test_browser_job_flow_and_cleanup(tmp_path, monkeypatch):
    """engine='browser': 区間を待ち → 受け取って → 書き出し、一時フォルダは消える（mmap を手放してから）。"""
    import threading
    import time as _t
    from djmix import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    pa, pb = tmp_path / "a.wav", tmp_path / "b.wav"
    click_track(pa, bpm=120, seconds=30)
    click_track(pb, bpm=120, seconds=30)
    tracks = [synth_track(0, 120, 30, pa, t0=1, t1=20, join_tpl="short", join_bars=4), synth_track(1, 120, 30, pb, t0=1, t1=20)]
    job = render.RenderJob([], {"tempo_range": 0.08, "lufs_target": -14}, tmp_path / "out.wav", "wav", True, None, "browser")
    monkeypatch.setattr(job, "load_tracks", lambda: tracks)
    th = threading.Thread(target=job._run)
    th.start()
    for _ in range(100):
        if job.stage == "need_audio":
            break
        _t.sleep(0.05)
    assert job.stage == "need_audio" and job.tmp.exists()
    for g in job.segment_info():
        n = len(g["pos"]) * g["step"]
        job.put_audio(g["id"], (np.ones((2, n), np.float32) * 0.05).tobytes())
    th.join(60)
    assert job.error is None and job.stage == "done" and (tmp_path / "out.wav").exists()
    assert not job.tmp.exists() and not list(tmp_path.glob("export_*"))
