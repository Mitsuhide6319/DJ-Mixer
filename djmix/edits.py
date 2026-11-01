"""ユーザー修正（user_edits）を解析結果に重ねる（design_phase1.md §3.2）。
解析結果（自動）は書き換えず、読み出すときに適用するので、再解析してもユーザーの修正は消えない。

- meter: 拍子の手動指定（Money の 7 拍子など。P1-3 で、1 曲 1 拍子の自動推定の限界として確認）
- downbeat_anchors: 「ここを 1 拍目に」。拍番号のリスト。その拍を含む区間（拍が途切れるまで）の小節位相を
  その拍に合わせる。曲全体ではなく区間単位にするのは、ジャズでは小節頭が曲中で揺らぐため（打合せ5）
- grid_offset_ms: 拍全体を前後にずらす
- bpm_scale: BPM の表示を ×½ / ×2 にする（曲全体の倍/半は自動補正しない方針。P1-3）。SYNC の倍/半は k で別に扱う
- hotcues / cue_point: ホットキュー 4 つとキュー位置（秒）
"""
from __future__ import annotations

import json

import numpy as np

from . import db
from .analysis import bars as bars_mod
from .analysis import cache
from .analysis.beats import gap_mask

EDIT_COLS = ("meter", "bpm_scale", "downbeat_anchors", "grid_offset_ms", "hotcues", "cue_point", "memo")


def get(track_id: int, db_path=None) -> dict:
    r = db.connect(db_path).execute("SELECT * FROM user_edits WHERE track_id = ?", (track_id,)).fetchone()
    if not r:
        return {}
    e = dict(r)
    for k in ("downbeat_anchors", "hotcues"):
        e[k] = json.loads(e[k]) if e.get(k) else None
    return e


def update(track_id: int, patch: dict, db_path=None) -> dict:
    """patch のキー: meter(None で自動に戻す), bpm_scale, grid_offset_ms, add_anchor(拍番号), clear_anchors, hotcues, cue_point。"""
    e = get(track_id, db_path)
    cur = {k: e.get(k) for k in EDIT_COLS}
    if "meter" in patch:
        m = patch["meter"]
        cur["meter"] = int(m) if m else None
    if "bpm_scale" in patch:
        cur["bpm_scale"] = float(patch["bpm_scale"]) if patch["bpm_scale"] in (0.5, 1, 2, 0.5, 1.0, 2.0) else 1.0
    if "grid_offset_ms" in patch:
        cur["grid_offset_ms"] = max(-200.0, min(200.0, float(patch["grid_offset_ms"])))
    if patch.get("clear_anchors"):
        cur["downbeat_anchors"] = None
    if patch.get("add_anchor") is not None:
        anchors = [a for a in (cur["downbeat_anchors"] or []) if abs(a - int(patch["add_anchor"])) > 2]
        cur["downbeat_anchors"] = sorted(anchors + [int(patch["add_anchor"])])
    if "hotcues" in patch:
        cur["hotcues"] = patch["hotcues"]
    if "cue_point" in patch:
        cur["cue_point"] = patch["cue_point"]
    with db.tx(db_path) as c:
        c.execute(
            """INSERT INTO user_edits(track_id, meter, bpm_scale, downbeat_anchors, grid_offset_ms, hotcues, cue_point, memo)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(track_id) DO UPDATE SET meter=excluded.meter, bpm_scale=excluded.bpm_scale,
                 downbeat_anchors=excluded.downbeat_anchors, grid_offset_ms=excluded.grid_offset_ms,
                 hotcues=excluded.hotcues, cue_point=excluded.cue_point, memo=excluded.memo""",
            (track_id, cur["meter"], cur["bpm_scale"] or 1.0,
             json.dumps(cur["downbeat_anchors"]) if cur["downbeat_anchors"] else None,
             cur["grid_offset_ms"] or 0.0, json.dumps(cur["hotcues"]) if cur["hotcues"] is not None else None,
             cur["cue_point"], cur["memo"]))
        # ライブラリ・キューの BPM 表示にも倍/半を反映する
        a = cache.read(track_id)
        if a is not None:
            c.execute("UPDATE tracks SET bpm = ? WHERE id = ?", (round(a["bpm"] * (cur["bpm_scale"] or 1.0), 2), track_id))
    return cur


def _anchor_bars(beats: np.ndarray, bars: list[dict], anchors: list[int], meter: int) -> list[dict]:
    """1 拍目の指定を反映する。指定した拍を含む区間（拍の途切れまで）で、指定の拍から前後に meter 拍ごとを小節頭にする。
    同じ区間に指定が複数あれば、それぞれの指定から次の指定の手前までをその指定に合わせる。"""
    n = len(beats)
    seg = np.concatenate([[0], np.cumsum(gap_mask(beats))])
    out = list(bars)
    for sid in sorted({int(seg[a]) for a in anchors if 0 <= a < n}):
        idx = np.where(seg == sid)[0]
        lo, hi = int(idx[0]), int(idx[-1])
        ancs = sorted(a for a in anchors if lo <= a <= hi)
        new = []
        for i, a in enumerate(ancs):
            start = lo if i == 0 else a
            end = ancs[i + 1] - 1 if i + 1 < len(ancs) else hi
            first = a - ((a - start) // meter) * meter
            for b in range(first, end + 1, meter):
                new.append({"beat": int(b), "time": round(float(beats[b]), 4), "conf": 1.0})
        out = [x for x in out if not (lo <= x["beat"] <= hi)] + new
    return sorted(out, key=lambda x: x["beat"])


def apply(track_id: int, analysis: dict, db_path=None) -> dict:
    e = get(track_id, db_path)
    out = dict(analysis)
    out["edits"] = {k: v for k, v in e.items() if k != "track_id" and v is not None}
    beats = np.array(analysis["beats"])
    meter = int(e.get("meter") or analysis["meter"])
    out["meter_source"] = "user" if e.get("meter") else "auto"
    if meter != analysis["meter"]:
        dp = cache.downbeat_prob(track_id)
        if dp is not None:
            out["bars"] = bars_mod.bar_phases(beats, bars_mod.downbeat_strength(beats, dp), meter)
    out["meter"] = meter
    anchors = e.get("downbeat_anchors") or []
    if anchors:
        out["bars"] = _anchor_bars(beats, out["bars"], anchors, meter)
    off = (e.get("grid_offset_ms") or 0.0) / 1000
    if off:
        out["beats"] = np.round(beats + off, 4).tolist()
        out["bars"] = [{**b, "time": round(b["time"] + off, 4)} for b in out["bars"]]
        out["tempo_curve"] = {**analysis["tempo_curve"], "t": [round(t + off, 3) for t in analysis["tempo_curve"]["t"]]}
    scale = e.get("bpm_scale") or 1.0
    out["bpm_scale"] = scale
    out["bpm_display"] = round(analysis["bpm"] * scale, 2)
    return out
