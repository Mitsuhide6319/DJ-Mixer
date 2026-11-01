"""繋ぎの計算の Python 版（frontend/engine/transition.js と同じ値を返す。tests/test_render.py で一致を確かめる）。"""
from __future__ import annotations

import math

TEMPLATES = {"long": 16, "short": 8, "bass": 16, "filter": 16, "echo": 2, "cut": 0}
DEFAULT_PARAMS = {"swapAt": 0.5, "xfStart": 0.0, "xfEnd": 1.0, "tempo": "sync", "loop": 0, "shift": 0, "parts": "none",
                  "cutTrim": "auto"}
PARTS = ("none", "drums", "vocal", "swap")      # パートごとに鳴らす（Phase 5 B2。transition.js の PARTS）


def uses_parts(join: dict | None) -> bool:
    return bool(join) and join["tpl"] in ("long", "short", "bass") and join["params"].get("parts") in PARTS[1:]


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def join_of(item: dict | None) -> dict:
    item = item or {}
    tpl = item.get("join_tpl") if item.get("join_tpl") in TEMPLATES else "long"
    bars = item.get("join_bars")
    bars = TEMPLATES[tpl] if bars is None else bars
    params = {**DEFAULT_PARAMS, **(item.get("join_params") or {})}
    return {"tpl": tpl, "bars": 0 if tpl == "cut" else max(1, min(64, int(bars))), "params": params}


def _step(u: float, at: float, w: float) -> float:
    return (1.0 if u >= at else 0.0) if w <= 0 else clamp01((u - at) / w + 0.5)


def levels(join: dict, u: float, beats: float = 64) -> dict:
    tpl, p = join["tpl"], join["params"]
    u = clamp01(u)
    one_beat = 1 / max(1, beats)
    neutral = uses_parts(join)      # パートごとに鳴らすときは EQ の LOW を動かさない（Phase 5 B2）
    swap_out = 1.0 if neutral else 1 - _step(u, p["swapAt"], one_beat)
    swap_in = 1.0 if neutral else _step(u, p["swapAt"], one_beat)
    last_bar = min(1, 4 * one_beat)
    if tpl == "bass":
        return {"xf": 0.5 if u < 1 - last_bar else 0.5 + 0.5 * clamp01((u - (1 - last_bar)) / last_bar),
                "lowOut": swap_out, "lowIn": swap_in, "hpOut": 0.0, "echoOut": 0.0}
    if tpl == "filter":
        return {"xf": 0.5 if u < 0.8 else 0.5 + 0.5 * clamp01((u - 0.8) / 0.2), "lowOut": 1 - u, "lowIn": clamp01(u * 2),
                "hpOut": u * 0.9, "echoOut": 0.0}
    if tpl == "echo":
        return {"xf": 0.0 if u < 1 - one_beat / 2 else 1.0, "lowOut": 1.0, "lowIn": 1.0, "hpOut": 0.0,
                "echoOut": clamp01((u - (1 - last_bar)) / last_bar)}
    if tpl == "cut":
        return {"xf": 0.0 if u < 1 else 1.0, "lowOut": 1.0, "lowIn": 1.0, "hpOut": 0.0, "echoOut": 0.0}
    span = max(1e-6, p["xfEnd"] - p["xfStart"])
    return {"xf": clamp01((u - p["xfStart"]) / span), "lowOut": swap_out, "lowIn": swap_in, "hpOut": 0.0, "echoOut": 0.0}


def part_gains(join: dict, u: float, beats: float = 64) -> dict | None:
    """パートごとの音量 {out: [v, d, b, o], in: [...]}（transition.js の partGains と同じ値）。"""
    if not uses_parts(join):
        return None
    p, mode = join["params"], join["params"]["parts"]
    u = clamp01(u)
    one_beat = 1 / max(1, beats)
    bar = min(0.25, 4 * one_beat)
    out, inn = [1.0] * 4, [1.0] * 4
    if mode in ("vocal", "swap"):
        out[0] = 1 - clamp01(u / 0.25)
    if mode == "swap":
        out[2] = 1 - _step(u, p["swapAt"], one_beat)
    if mode in ("drums", "swap"):
        inn[3] = clamp01((u - 0.25) / max(bar, 0.125))
        inn[2] = _step(u, p["swapAt"], one_beat)
        inn[0] = clamp01((u - 0.75) / 0.25)
    return {"out": out, "in": inn}


def overlap_start(bar_times: list[float], out_end: float, bars: int) -> dict:
    end = 0
    for i in range(len(bar_times)):
        if abs(bar_times[i] - out_end) < abs(bar_times[end] - out_end):
            end = i
    start = max(0, end - bars)
    return {"startTime": bar_times[start], "startBar": start, "endBar": end}


def in_start_bar(bar_times: list[float], t0: float) -> int:
    b = 0
    for i in range(1, len(bar_times)):
        if abs(bar_times[i] - t0) < abs(bar_times[b] - t0):
            b = i
    return b


def enters_at_end(tpl: str) -> bool:
    return tpl in ("echo", "cut")


def judge(bm_out, bm_in, ov_start: float, ov_end: float, in_start: float, rate_out: float = 1.0) -> dict:
    bpm_out = 60 / bm_out.beat_len(ov_start) * rate_out
    bpm_in = 60 / bm_in.beat_len(in_start)
    best = None
    for k in (1.0, 2.0, 0.5):
        rate = k * bpm_out / bpm_in
        if best is None or abs(math.log(rate)) < abs(math.log(best[1])):
            best = (k, rate)
    k, rate = best
    reason = None
    if rate < 0.7 or rate > 1.3:
        reason = f"テンポの差が大きすぎます（{round((rate - 1) * 100)}%）"
    else:
        t = ov_start
        while t < ov_end:
            if bm_out.in_no_beat(t):
                reason = "前の曲の繋ぐ部分に拍がありません"
                break
            t += 0.25
        if not reason:
            t, end = in_start, in_start + max(ov_end - ov_start, 2)
            while t < end:
                if bm_in.in_no_beat(t):
                    reason = "次の曲の入る部分に拍がありません"
                    break
                t += 0.25
    return {"k": k, "rate": rate, "reason": reason}


def synced_rate(bm_in, bm_out, pos_in: float, pos_out: float, k: float, rate_out: float = 1.0) -> float:
    """繋ぎの終わりの次の曲の速度（直前 4 拍の平均。transition.js の syncedRate と同じ式）。"""
    p_i, p_o = bm_in.pos(pos_in), bm_out.pos(pos_out)
    in_sec = bm_in.time(p_i) - bm_in.time(p_i - 4 * k)
    out_sec = bm_out.time(p_o) - bm_out.time(p_o - 4)
    return in_sec * rate_out / out_sec if out_sec > 0 else 1.0


def overlap_bars(bar_times: list[float], out_end: float, join: dict) -> dict:
    """重なりの小節（ループを含む。transition.js の overlapBars と同じ）。"""
    loop = 0 if join["tpl"] in ("echo", "cut") else max(0, min(int(join["params"].get("loop") or 0), join["bars"] - 1))
    ov = overlap_start(bar_times, out_end, join["bars"] - loop)
    return {**ov, "loop": loop, "loopStartBar": ov["endBar"] - loop}


def ramp_target(bm_out, bm_in, pos_out_end: float, pos_in_end: float, k: float) -> float:
    p_o, p_i = bm_out.pos(pos_out_end), bm_in.pos(pos_in_end)
    out_sec = bm_out.time(p_o) - bm_out.time(p_o - 4)
    in_sec = bm_in.time(p_i) - bm_in.time(p_i - 4 * k)
    return out_sec / in_sec if in_sec > 0 else 1.0
