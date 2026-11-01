"""P1-7: ユーザー修正（1 拍目の指定・グリッドのずらし・拍子・BPM 表示）。"""
import numpy as np

from djmix import db, edits


def make_analysis():
    # 0〜20 秒は 0.5 秒間隔、20〜25 秒は拍なし、25〜45 秒は 0.5 秒間隔
    beats = np.r_[np.arange(0, 20, 0.5), np.arange(25, 45, 0.5)]
    bars = [{"beat": i, "time": float(beats[i]), "conf": 0.2} for i in range(1, len(beats), 4)]  # 自動推定は 2 拍目にずれている
    return {"beats": beats.tolist(), "bars": bars, "meter": 4, "bpm": 120.0,
            "tempo_curve": {"t": beats.tolist(), "bpm": [120.0] * len(beats)}}


def test_anchor_rephases_only_its_segment(tmp_path):
    dbp = tmp_path / "t.sqlite"
    with db.tx(dbp) as c:
        c.execute("INSERT INTO tracks(id, path, folder) VALUES(1, 'x', '')")
    a = make_analysis()
    edits.update(1, {"add_anchor": 8}, dbp)                 # 前半の区間の 8 拍目（4 秒）を 1 拍目に
    out = edits.apply(1, a, dbp)
    first = [b["beat"] for b in out["bars"] if b["beat"] < 40]
    assert first == list(range(0, 40, 4)) and all(b["conf"] == 1.0 for b in out["bars"] if b["beat"] < 40)
    second = [b["beat"] for b in out["bars"] if b["beat"] >= 40]
    assert second == [b["beat"] for b in a["bars"] if b["beat"] >= 40]   # 拍なし区間の後ろは自動のまま


def test_two_anchors_in_one_segment(tmp_path):
    dbp = tmp_path / "t.sqlite"
    with db.tx(dbp) as c:
        c.execute("INSERT INTO tracks(id, path, folder) VALUES(1, 'x', '')")
    a = make_analysis()
    edits.update(1, {"add_anchor": 0}, dbp)
    edits.update(1, {"add_anchor": 22}, dbp)                # 途中で位相が変わる（22 から 4 拍ごと）
    beats = [b["beat"] for b in edits.apply(1, a, dbp)["bars"] if b["beat"] < 40]
    assert beats == [0, 4, 8, 12, 16, 20, 22, 26, 30, 34, 38]
    edits.update(1, {"clear_anchors": True}, dbp)
    assert [b["beat"] for b in edits.apply(1, a, dbp)["bars"]] == [b["beat"] for b in a["bars"]]


def test_grid_offset_and_bpm_scale(tmp_path):
    dbp = tmp_path / "t.sqlite"
    with db.tx(dbp) as c:
        c.execute("INSERT INTO tracks(id, path, folder) VALUES(1, 'x', '')")
    a = make_analysis()
    edits.update(1, {"grid_offset_ms": 15, "bpm_scale": 0.5}, dbp)
    out = edits.apply(1, a, dbp)
    assert abs(out["beats"][3] - (1.5 + 0.015)) < 1e-6 and abs(out["bars"][0]["time"] - (0.5 + 0.015)) < 1e-6
    assert out["bpm_display"] == 60.0 and out["bpm"] == 120.0
    edits.update(1, {"hotcues": [12.5, None, None, 30.0], "cue_point": 4.0}, dbp)
    e = edits.get(1, dbp)
    assert e["hotcues"] == [12.5, None, None, 30.0] and e["cue_point"] == 4.0 and e["grid_offset_ms"] == 15
