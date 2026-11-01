"""高速で書き出す（design_phase3.md §6.2）。再生せずに、キュー（使う範囲・つなぎ・設定）からミックスを計算する。

再生（frontend/engine/runner.js・sync.js・mixer.js）と同じ規則を、512 サンプル（約 11.6ms）刻みの「操作の表」として計算し、
その表どおりに各曲をストレッチ（Signalsmith の Python 版。再生と同じエンジン）してミキサーに通す。

1. 拍の写像: 前の曲に位相を固定して入る（次の曲の入り口の小節頭 ＝ 重なりの始まりの小節頭。エコーアウト・カットは前の曲の終わり）、
   重なりの間は 次の曲の拍位置 = phi + k × 前の曲の拍位置（再生では 20ms ごとの追従で近づける値を、ここでは正確に計算する）、
   繋ぎの後は 8 小節かけて原速へ（テンポフェーダーの幅で頭打ち。runner と同じ）、次の重なりが近ければ打ち切る
2. 自動の動き: transition.py（transition.js の levels() と同じ値）
3. ミキサー: 音量補正 → 3 バンドのアイソレーター（LR4 250Hz/2.5kHz＋オールパス補正）→ フィルター → クロスフェーダー（カーブ）
   → エコー（フェーダーの前から分岐）→ マスター → 先読みのリミッター（−1dBFS を超えない）
手で動かした操作は入らない。チャンネルフェーダーは全開、GAIN は 0dB として計算する。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal

from .. import db, edits
from ..analysis import cache
from ..audio.decode import SR, decode
from . import transition as TR
from .writer import WavSink

log = logging.getLogger("djmix.render")

STEP = 512                      # 操作の表の刻み（サンプル）
MAX_BATCH = 16                  # まとめてストレッチする刻みの数（8192 サンプル）
STRETCH_GAIN = 10 ** (0.18 / 20)  # python-stretch の出力がわずかに小さい分（8192 サンプルずつで約 −0.18dB）の補い
DT = STEP / SR
XO_LOW, XO_HIGH = 250.0, 2500.0
RETURN_BARS = 8
LIMIT = 10 ** (-1.0 / 20)       # リミッターの上限（−1dBFS）
RELEASE_PER_STEP = 10 ** (6 / 20 * DT / 0.25)   # 解除の速さ: 0.25 秒で 6dB 戻す


# ---------------------------------------------------------------- 拍の位置（frontend/engine/beatmap.js と同じ）
class BeatMap:
    def __init__(self, a: dict):
        self.b = np.asarray(a["beats"], dtype=np.float64)
        self.meter = a["meter"]
        self.bar_times = [x["time"] for x in a["bars"]]
        self.no_beat = a["no_beat"]
        self.n = len(self.b)

    def pos(self, t: float) -> float:
        b, n = self.b, self.n
        if t <= b[0]:
            return (t - b[0]) / (b[1] - b[0])
        if t >= b[-1]:
            return n - 1 + (t - b[-1]) / (b[-1] - b[-2])
        i = int(np.searchsorted(b, t, "left")) - 1
        return i + (t - b[i]) / (b[i + 1] - b[i])

    def time(self, p: float) -> float:
        b, n = self.b, self.n
        if p <= 0:
            return b[0] + p * (b[1] - b[0])
        if p >= n - 1:
            return b[-1] + (p - (n - 1)) * (b[-1] - b[-2])
        i = int(p)
        return b[i] + (p - i) * (b[i + 1] - b[i])

    def beat_len(self, t: float) -> float:
        i = max(0, min(self.n - 2, int(np.searchsorted(self.b, t, "left")) - 1))
        return float(self.b[i + 1] - self.b[i])

    def in_no_beat(self, t: float) -> bool:
        return any(s <= t < e for s, e in self.no_beat)


# ---------------------------------------------------------------- 操作の表（runner.js の規則）
@dataclass
class Track:
    idx: int
    item: dict
    a: dict
    bm: BeatMap
    t0: float
    t1: float
    lufs: float
    title: str = ""
    artist: str = ""
    album: str = ""
    parts_ok: bool = False        # 分離した音があり、パートごとに鳴らせる（Phase 5 B2）
    shift: float = 0.0            # キー合わせ（半音。この曲が次の曲として入る繋ぎで決まる。Phase 4）
    cut_bars: list = field(default_factory=list)   # 小節ごとの音量 [{t, db}]（カットインの音量合わせ。analysis/cutpoints.py）


@dataclass
class Plan:
    """操作の表。デッキ 0/1 ごとに、刻みごとの 曲の番号（-1 = 止まっている）・入力位置（秒）・LOW・ハイパス・エコーの送り、と共通のクロスフェーダー。"""
    n: int
    track: np.ndarray = field(default=None)
    pos: np.ndarray = field(default=None)
    low: np.ndarray = field(default=None)
    hp: np.ndarray = field(default=None)
    echo: np.ndarray = field(default=None)
    echo_delay: np.ndarray = field(default=None)
    xf: np.ndarray = field(default=None)
    pm: np.ndarray = field(default=None)          # パートの音で鳴らすか（0/1。Phase 5 B2）
    pg: np.ndarray = field(default=None)          # パートごとの音量（刻み × 4）
    jg: np.ndarray = field(default=None)          # カットインの音量合わせ（倍率。design_cutin.md ③）
    cuts: list = field(default_factory=list)      # 次の曲を前の曲の終わりで入れた所 {k: 刻み, f: 刻みの中の位置 0..1, out: 前の曲のデッキ}
    marks: list = field(default_factory=list)


def _judge_join(out: Track, inn: Track, rate_out: float, target: float = -14.0) -> dict:
    """runner.plan() と同じ計画。"""
    j = TR.join_of(out.item)
    bmO, bmI = out.bm, inn.bm

    def geo(jj):
        ov = TR.overlap_bars(bmO.bar_times, out.t1, jj)          # ループ（Phase 4）を含む
        end = bmO.bar_times[ov["endBar"]] if ov["endBar"] < len(bmO.bar_times) else out.t1
        return ov, ov["startTime"], end
    ov, s, e = geo(j)
    in_bar = TR.in_start_bar(bmI.bar_times, inn.t0)
    in_time = bmI.bar_times[in_bar] if bmI.bar_times else inn.t0
    jd = TR.judge(bmO, bmI, s, e, in_time, rate_out)
    if jd["reason"] and j["tpl"] not in ("echo", "cut"):
        j = {**j, "tpl": "echo", "bars": 2}
        ov, s, e = geo(j)
    synced = not jd["reason"] and j["tpl"] != "cut"
    late = (not synced) or TR.enters_at_end(j["tpl"])
    from ..analysis import cutpoints
    cut = j["tpl"] == "cut"
    trim = 0.0
    if cut:                       # カットインの入りの音量合わせ（runner と同じ。design_cutin.md ③）
        bar_o = bmO.beat_len(e) * (bmO.meter or 4)
        bar_i = bmI.beat_len(in_time) * (bmI.meter or 4)
        auto = cutpoints.auto_trim_db(out.cut_bars, e, bar_o, out.lufs, inn.cut_bars, in_time, bar_i, inn.lufs, target)
        trim = cutpoints.trim_db(j["params"], auto)
    pS, pE = bmO.pos(s), bmO.pos(e)
    phi = bmI.pos(in_time) - jd["k"] * (pE if late else pS)
    loop = 0 if late else ov["loop"]
    loop_start = bmO.bar_times[ov["loopStartBar"]] if loop else None
    loop_beats = pE - bmO.pos(loop_start) if loop else 0.0
    beats = max(1.0, pE - pS + loop_beats)
    ramp_to = (TR.ramp_target(bmO, bmI, e, bmI.time(bmI.pos(in_time) + jd["k"] * beats), jd["k"])
               if not late and j["params"].get("tempo") == "ramp" else None)
    inn.shift = float(j["params"].get("shift") or 0)
    parts = j["params"]["parts"] if (not late and TR.uses_parts(j) and out.parts_ok and inn.parts_ok) else None
    return {"join": j, "parts": parts, "ovStart": s, "ovEnd": e, "pS": pS, "pE": pE, "beats": beats, "k": jd["k"], "phi": phi,
            "synced": synced, "late": late, "in_time": in_time if synced or cut else inn.t0, "reason": jd["reason"], "cut": cut, "trim": trim,
            "trim_sec": cutpoints.TRIM_BACK_BARS * bmI.beat_len(in_time) * (bmI.meter or 4) if trim else 0.0,
            "loop": loop, "loopStart": loop_start, "loopEnd": e if loop else None, "loopBeats": loop_beats, "rampTo": ramp_to}


def build_plan(tracks: list[Track], settings: dict) -> Plan:
    tempo_range = float(settings.get("tempo_range", 0.08))
    tempo_return = settings.get("tempo_return", True) is not False
    cols = {k: [[], []] for k in ("track", "pos", "low", "hp", "echo", "echo_delay", "pm", "pg", "jg")}
    cuts = []
    target = float(settings.get("lufs_target", -14))
    xf_col: list[float] = []
    marks = []

    # デッキの状態
    deck = [{"t": None, "pos": 0.0, "rate": 1.0, "low": 1.0, "hp": 0.0, "echo": 0.0, "delay": 0.3, "pm": 0, "pg": [1.0] * 4,
             "jg": None} for _ in range(2)]
    xf = 0.0                                  # 0 = デッキ0 側
    cur_d = 0
    first = tracks[0]
    deck[0].update(t=first, pos=first.t0)
    marks.append({"t": 0.0, "title": first.title, "artist": first.artist, "album": first.album})
    n_step = 0
    ramp = None                               # 原速へ戻す {from, start(beat), beats}
    plan = None                               # いまの繋ぎの計画
    phase = "solo"                            # solo（次の繋ぎ待ち）/ pre / overlap / post
    i = 0                                     # 主になっている曲の番号

    def emit():
        for d in range(2):
            st = deck[d]
            cols["track"][d].append(st["t"].idx if st["t"] is not None and st.get("playing", d == cur_d) else -1)
            cols["pos"][d].append(st["pos"])
            cols["low"][d].append(st["low"]); cols["hp"][d].append(st["hp"])
            cols["echo"][d].append(st["echo"]); cols["echo_delay"][d].append(st["delay"])
            cols["pm"][d].append(st["pm"]); cols["pg"][d].append(list(st["pg"]))
            jg = 1.0
            if st["jg"]:                # 入りの音量合わせ: 切り替えの時刻に g、そこから直線で 1 へ（mixer.js の setJoinGainAt と同じ）
                g0, ta, sec = st["jg"]
                jg = g0 + (1 - g0) * min(1.0, max(0.0, (n_step * DT - ta) / sec))
            cols["jg"][d].append(jg)
        xf_col.append(xf)

    deck[0]["playing"] = True
    deck[1]["playing"] = False

    def arm():
        nonlocal plan, phase, ramp, xf
        out, inn = tracks[i], tracks[i + 1]
        xf = 0.0 if cur_d == 0 else 1.0        # runner の arm と同じ: 前の曲だけが聞こえる状態
        plan = _judge_join(out, inn, deck[cur_d]["rate"], target)
        other = 1 - cur_d
        deck[other].update(t=inn, pos=plan["in_time"], rate=1.0, low=1.0, hp=0.0, echo=0.0, playing=False, pm=0, pg=[1.0] * 4, jg=None)
        deck[cur_d].update(low=1.0, hp=0.0, echo=0.0)
        ramp = None                            # 次の繋ぎを待機させたら、原速へ戻すのは打ち切る（runner と同じ）
        phase = "pre"

    if len(tracks) > 1:
        arm()
    while True:
        O = deck[cur_d]
        other = 1 - cur_d
        I = deck[other]
        out = tracks[i]
        # ---- 主の曲の進み ----
        if ramp is not None:
            p = min(1.0, max(0.0, (out.bm.pos(O["pos"]) - ramp["start"]) / ramp["beats"]))
            r = ramp["from"] + (1 - ramp["from"]) * p
            tf = max(-1.0, min(1.0, (r - 1) / tempo_range))
            O["rate"] = 1 + tf * tempo_range
            if p >= 1:
                ramp = None
        if phase in ("pre", "overlap"):
            pl = plan
            # 前の曲の終わりで次の曲を入れる繋ぎ（カットイン・エコーアウト）: 前の曲が小節の頭に来るのは、この刻みの f の位置。
            # 次の曲の入り口がちょうどそこで鳴るよう、刻みの頭の位置を f だけ手前にする（刻みの f より前の音は Mixdown で消す）
            if pl["late"] and not I["playing"] and O["pos"] >= pl["ovEnd"] - DT * O["rate"]:
                f = min(1.0, max(0.0, (pl["ovEnd"] - O["pos"]) / (max(1e-6, O["rate"]) * DT)))
                I["playing"], I["just"] = True, True
                pl["start_k"] = n_step
                if not pl["synced"]:
                    I["pos"] = pl["in_time"] - f * DT
                if pl["trim"]:
                    I["jg"] = (10 ** (pl["trim"] / 20), (n_step + f) * DT, pl["trim_sec"])
                cuts.append({"k": n_step, "f": f, "out": cur_d})
                marks.append({"t": (n_step + f) * DT, "title": tracks[i + 1].title, "artist": tracks[i + 1].artist, "album": tracks[i + 1].album})
            if phase == "pre" and O["pos"] >= pl["ovStart"]:
                phase = "overlap"
                pl["acc"] = max(0.0, out.bm.pos(O["pos"]) - pl["pS"])      # 拍の進みを足し合わせる（ループで巻き戻るため）
                pl["rampFrom"] = O["rate"]
                if not pl["late"]:
                    I["playing"] = True
                    marks.append({"t": n_step * DT, "title": tracks[i + 1].title, "artist": tracks[i + 1].artist, "album": tracks[i + 1].album})
            if phase == "overlap":
                bmO = out.bm
                u = min(1.0, max(0.0, pl["acc"] / pl["beats"]))
                if pl["rampTo"]:           # テンポランプ: 前の曲の速度を直線で変える（runner と同じ）
                    O["rate"] = pl["rampFrom"] + (pl["rampTo"] - pl["rampFrom"]) * u
                lv = TR.levels(pl["join"], u, pl["beats"])
                xf_target = lv["xf"]
                xf = xf_target if cur_d == 0 else 1 - xf_target
                O["low"], O["hp"], O["echo"] = lv["lowOut"], lv["hpOut"], lv["echoOut"]
                I["low"] = lv["lowIn"]
                O["delay"] = bmO.beat_len(O["pos"]) / max(1e-6, O["rate"]) * 0.75
                if pl["parts"]:            # パートごとの音量（runner と同じ。前の曲は重なりの頭でパートの音へ）
                    pg = TR.part_gains(pl["join"], u, pl["beats"])
                    O["pm"], O["pg"] = 1, pg["out"]
                    I["pm"], I["pg"] = 1, pg["in"]
                if pl["late"]:             # 次の曲を入れた刻みの次で（入れた刻みの f までは前の曲が鳴る）
                    ended = pl.get("start_k") is not None and n_step > pl["start_k"]
                else:
                    ended = pl["acc"] >= pl["beats"] - 0.02 if (pl["loop"] or pl["rampTo"]) else O["pos"] >= pl["ovEnd"] - 0.01
                if ended:
                    # 繋ぎ終わり: 前の曲を止め、次の曲を主にする
                    lv = TR.levels(pl["join"], 1.0, pl["beats"])
                    xf = lv["xf"] if cur_d == 0 else 1 - lv["xf"]
                    O["playing"] = False
                    O["echo"] = 0.0
                    O["low"], O["hp"] = 1.0, 0.0
                    O["pm"], O["pg"] = 0, [1.0] * 4
                    I["pm"], I["pg"] = 0, [1.0] * 4         # 次の曲は全パート 1 になっているので元の音へ戻す
                    # この刻みの次の曲の位置も進めておく（主が入れ替わると「従」の位置の計算を通らず、1 刻み止まっていた）
                    if pl["synced"]:
                        I["pos"] = tracks[i + 1].bm.time(pl["phi"] + pl["k"] * (pl["pS"] + pl["acc"]))
                    elif not I.pop("just", False):
                        I["pos"] += DT
                    I["low"], I["playing"] = lv["lowIn"] if lv["lowIn"] else 1.0, True
                    # 次の曲の今の速度（位相を固定した写像の傾き）
                    if pl["synced"]:       # 拍を合わせていた速度（直前 4 拍の平均）をテンポフェーダーの幅で頭打ちにする（runner と同じ）
                        r = TR.synced_rate(tracks[i + 1].bm, out.bm, I["pos"], O["pos"], pl["k"], O["rate"])
                        r = 1 + max(-1.0, min(1.0, (r - 1) / tempo_range)) * tempo_range
                        if pl["rampTo"]:
                            r = 1.0            # テンポランプなら次の曲はもう原速
                    else:
                        r = 1.0
                    I["rate"] = r
                    cur_d, i = other, i + 1
                    phase = "post"
                    if tempo_return and abs(r - 1) > 0.002:
                        bm = tracks[i].bm
                        ramp = {"from": r, "start": bm.pos(I["pos"]), "beats": RETURN_BARS * (bm.meter or 4)}
        # ---- 次の曲（従）の位置 ----
        if phase in ("pre", "overlap") and deck[1 - cur_d]["playing"]:
            pl = plan
            S = deck[1 - cur_d]
            if pl["synced"]:       # 前の曲の拍位置は足し合わせた値で（ループで巻き戻っても次の曲は進み続ける）
                p_out = pl["pS"] + pl["acc"] if phase == "overlap" else tracks[i].bm.pos(deck[cur_d]["pos"])
                S["pos"] = tracks[i + 1].bm.time(pl["phi"] + pl["k"] * p_out)
            elif not S.pop("just", False):
                S["pos"] += DT
        emit()
        n_step += 1
        # ---- 時間を進める ----
        M = deck[cur_d]
        p0 = tracks[i].bm.pos(M["pos"]) if phase == "overlap" else None
        M["pos"] += M["rate"] * DT
        if phase == "overlap":
            pl = plan
            wrapped = False
            if pl["loop"] and pl["loopStart"] <= M["pos"] - M["rate"] * DT and M["pos"] >= pl["loopEnd"]:
                M["pos"] -= pl["loopEnd"] - pl["loopStart"]            # ループ: 最後の小節を繰り返す
                wrapped = True
            d = tracks[i].bm.pos(M["pos"]) - p0
            pl["acc"] += max(0.0, d + (pl["loopBeats"] if wrapped else 0.0))
        if phase == "post":
            # 次の繋ぎを待機させる: 原速へ戻し終えたら、または次の重なりが近いとき（runner と同じ）
            if i + 1 < len(tracks):
                t = tracks[i]
                bar_sec = t.bm.beat_len(M["pos"]) * (t.bm.meter or 4) / max(1e-6, M["rate"])
                soon = t.t1 - M["pos"] < (TR.join_of(t.item)["bars"] + 4) * bar_sec
                if ramp is None or soon:
                    arm()
            else:
                phase = "last"
        if phase in ("last", "solo") or (len(tracks) == 1):
            if M["pos"] >= tracks[i].t1 - 0.01:
                break
        if phase in ("pre",) and M["pos"] >= tracks[i].t1 + 5:     # 念のため（計画が壊れていても止まる）
            break
        if n_step > 6 * 3600 / DT:
            break
    plan_out = Plan(n=n_step, marks=marks, cuts=cuts)
    for k, v in cols.items():
        setattr(plan_out, k, [np.array(v[0]), np.array(v[1])])
    plan_out.xf = np.array(xf_col)
    return plan_out


# ---------------------------------------------------------------- 画面のストレッチで計算する区間（2026-10-02）
# python-stretch は 1 回の呼び出しごとに独立して処理する（前の呼び出しの続きにならない）ため、8192 サンプルごとの区切りで
# 音量が 1〜4dB 落ち、高速書き出しに「途切れるようなノイズ」が出た。区切りを重ねてつなぐ方法は、速度が 1 でないと位相がそろわず使えない。
# そこで、再生と同じ Signalsmith Stretch（画面の WebAssembly）を OfflineAudioContext で音を出さずに動かし、デッキごと・曲ごとの区間を
# 操作の表どおりの位置・速度で計算してもらう。ここではその区間の一覧を作る。
PRIME_STEPS = 176                 # パートの音は、使う所の少し前（約 2 秒）から計算しておく


def segments_of(plan: Plan, tracks: list[Track]) -> list[dict]:
    """デッキごとに、同じ曲が続く区間（main、2ch）と、パートの音を使う区間（stems、8ch）の一覧。"""
    segs = []
    for d in range(2):
        tr = plan.track[d]
        s = 0
        while s < plan.n:
            if tr[s] < 0:
                s += 1
                continue
            idx = int(tr[s])
            e = s
            while e < plan.n and tr[e] == idx:
                e += 1
            t = tracks[idx]
            base = {"deck": d, "idx": idx, "track_id": int(t.item["track_id"]), "shift": float(t.shift or 0)}
            segs.append({**base, "kind": "main", "channels": 2, "s0": s, "s1": e})
            on = np.where(plan.pm[d][s:e] > 0)[0] + s
            if len(on) and t.parts_ok:
                groups, g0 = [], on[0]
                for a, b in zip(on, on[1:]):
                    if b - a > 2 * PRIME_STEPS:
                        groups.append((g0, a))
                        g0 = b
                groups.append((g0, on[-1]))
                for a, b in groups:
                    segs.append({**base, "kind": "stems", "channels": 8, "s0": max(s, int(a) - PRIME_STEPS), "s1": min(e, int(b) + 3)})
            s = e
    for k, g in enumerate(segs):
        g["id"] = k
    return segs


def parts_tracks(tracks: list[Track]) -> set[int]:
    """パートごとに鳴らす繋ぎに使う曲（tracks の番号）。分離した音を用意するのに使う。"""
    out = set()
    for a, b in zip(tracks, tracks[1:]):
        if TR.uses_parts(TR.join_of(a.item)):
            out |= {a.idx, b.idx}
    return out


# ---------------------------------------------------------------- ミキサー（mixer.js と同じ構成）
def _rbj(kind: str, f: float, q: float) -> np.ndarray:
    """Web Audio の BiquadFilter と同じ式（RBJ）の 1 段。sos 形式。"""
    w0 = 2 * math.pi * min(f, SR / 2 * 0.999) / SR
    c, al = math.cos(w0), math.sin(w0) / (2 * q)
    if kind == "lowpass":
        b = [(1 - c) / 2, 1 - c, (1 - c) / 2]
    elif kind == "highpass":
        b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
    else:                       # allpass
        b = [1 - al, -2 * c, 1 + al]
    a = [1 + al, -2 * c, 1 - al]
    return np.array([[b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]]])


Q_B = 1 / math.sqrt(2)
Q_F = 10 ** (0.9 / 20)            # フィルターの Q（Web Audio は dB 指定で 0.9）
BANDS = {
    "low": np.vstack([_rbj("lowpass", XO_LOW, Q_B), _rbj("lowpass", XO_LOW, Q_B), _rbj("allpass", XO_HIGH, Q_B)]),
    "mid": np.vstack([_rbj("highpass", XO_LOW, Q_B), _rbj("highpass", XO_LOW, Q_B), _rbj("lowpass", XO_HIGH, Q_B), _rbj("lowpass", XO_HIGH, Q_B)]),
    "high": np.vstack([_rbj("highpass", XO_LOW, Q_B), _rbj("highpass", XO_LOW, Q_B), _rbj("highpass", XO_HIGH, Q_B), _rbj("highpass", XO_HIGH, Q_B)]),
}
STATIC_FILTER = np.vstack([_rbj("lowpass", 22000, Q_F), _rbj("highpass", 10, Q_F)])


def hp_freq(hp_amount: float) -> float:
    """自動の動きのハイパス量（0..1）→ フィルターつまみ → 周波数（mixer.js の setFilter と同じ）。"""
    v, dz = 0.5 + 0.5 * hp_amount, 0.03
    return 10 * (10000 / 10) ** ((v - 0.5 - dz) / (0.5 - dz)) if v > 0.5 + dz else 10.0


def xfade_gains(x: np.ndarray, curve: str) -> tuple[np.ndarray, np.ndarray]:
    if curve == "linear":
        return 1 - x, x
    if curve == "cut":
        return np.minimum(1, (1 - x) / 0.06), np.minimum(1, x / 0.06)
    return np.cos(x * math.pi / 2), np.sin(x * math.pi / 2)


def _per_sample(v: np.ndarray) -> np.ndarray:
    """刻みごとの値を、刻みの間は直線でつないでサンプルごとに（ジッパーノイズを防ぐ）。"""
    nxt = np.append(v[1:], v[-1])
    ramp = np.arange(STEP) / STEP
    return (v[:, None] + (nxt - v)[:, None] * ramp[None, :]).reshape(-1)


class Channel:
    def __init__(self):
        self.zi = {k: np.zeros((len(sos), 2, 2)) for k, sos in BANDS.items()}
        self.zf = np.zeros((2, 2, 2))
        self.hp_zi = np.zeros((1, 2, 2))

    def process(self, x: np.ndarray, trim: float, low: np.ndarray, hp: np.ndarray) -> np.ndarray:
        """x: (n, 2)。low・hp は刻みごとの値（LOW の量 0..1、ハイパスの量 0..1）。"""
        x = x * trim
        out = np.zeros_like(x)
        for k, sos in BANDS.items():
            y, self.zi[k] = signal.sosfilt(sos, x, axis=0, zi=self.zi[k])
            if k == "low":
                y = y * (_per_sample(low) ** 2)[:, None]          # lowKnob(level)=0.5*level → eqGain = level²
            out += y
        out, self.zf = signal.sosfilt(STATIC_FILTER, out, axis=0, zi=self.zf)
        if np.any(hp > 0):                                        # フィルターの繋ぎ: 刻みごとに周波数を変える
            for s in range(len(hp)):
                seg = out[s * STEP:(s + 1) * STEP]
                out[s * STEP:(s + 1) * STEP], self.hp_zi = signal.sosfilt(_rbj("highpass", hp_freq(hp[s]), Q_F), seg, axis=0, zi=self.hp_zi)
        return out


class Echo:
    """フェーダーの前から分岐する遅延＋フィードバック（0.55）＋ 6kHz のローパス。遅延は 1 刻みより長い前提でまとめて処理する。"""

    def __init__(self):
        self.buf = np.zeros((SR * 5, 2))
        self.w = 0
        self.lp = _rbj("lowpass", 6000, 1.0)       # Web Audio の既定の Q（1 = 0dB）
        self.zi = np.zeros((1, 2, 2))
        self.active = False

    def process(self, send_in: np.ndarray, delay_sec: np.ndarray) -> np.ndarray:
        n = len(send_in)
        out = np.zeros_like(send_in)
        if not self.active and not send_in.any():
            return out                              # まだ一度も送っていなければ何もしない（速さのため）
        self.active = True
        L = len(self.buf)
        for s in range(n // STEP):
            d = max(STEP, min(L - STEP, int(round(delay_sec[s] * SR))))
            delayed = self.buf[(self.w - d + np.arange(STEP)) % L]
            y, self.zi = signal.sosfilt(self.lp, delayed, axis=0, zi=self.zi)
            out[s * STEP:(s + 1) * STEP] = y
            self.buf[(self.w + np.arange(STEP)) % L] = send_in[s * STEP:(s + 1) * STEP] + 0.55 * y
            self.w = (self.w + STEP) % L
        if np.abs(self.buf).max() < 1e-5:
            self.active = False                     # 余韻が消えた
        return out


class Compressor:
    """再生側のリミッター（Web Audio の DynamicsCompressor: しきい値 −1.5dB・比 20・ニー 0・アタック 2ms・リリース 120ms）に近い動き。
    DynamicsCompressor はこの設定で約 +0.86dB の持ち上げ（makeup gain）も掛けるので同じだけ掛ける。
    細かい山は通すため、最後に 0dBFS の手前で止める安全装置（Limiter）を通す（P3-8 の比較: 硬いリミッターだけだと、山の多い曲で
    再生より 2dB 小さくなった）。"""

    BLOCK = 64
    THR_DB, RATIO = -1.5, 20.0
    MAKEUP = 10 ** (0.855 / 20)

    def __init__(self):
        self.att = math.exp(-self.BLOCK / (0.002 * SR))
        self.rel = math.exp(-self.BLOCK / (0.12 * SR))
        self.env = 0.0
        self.g_prev = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x) // self.BLOCK * self.BLOCK
        peaks = np.abs(x[:n]).reshape(-1, self.BLOCK, 2).max(axis=(1, 2))
        gains = np.empty(len(peaks))
        env, att, rel, thr, slope = self.env, self.att, self.rel, self.THR_DB, 1 - 1 / self.RATIO
        for b, pk in enumerate(peaks):
            env = pk + (env - pk) * (att if pk > env else rel)
            over = 20 * math.log10(max(env, 1e-9)) - thr
            gains[b] = 10 ** (-over * slope / 20) if over > 0 else 1.0
        self.env = env
        prev = np.concatenate([[self.g_prev], gains[:-1]])
        ramp = (prev[:, None] + (gains - prev)[:, None] * (np.arange(self.BLOCK) / self.BLOCK)[None, :]).reshape(-1)
        self.g_prev = gains[-1] if len(gains) else self.g_prev
        return x[:n] * (ramp * self.MAKEUP)[:, None]


class Limiter:
    """最後の安全装置（サンプル単位の先読み）: 1.5ms 先の山まで見て上限（WAV は −0.3dBFS、MP3 は −1dBFS）を超えないよう下げ、約 50ms で戻す。
    MP3 は変換で山がはみ出す（−0.3dBFS で止めても、MP3 を読み戻すと 1.03 になった）ので低めにする。"""

    LOOK = 64

    def __init__(self, ceil_db: float = -0.3):
        self.CEIL = 10 ** (ceil_db / 20)
        from scipy.ndimage import minimum_filter1d
        self._minf = minimum_filter1d
        self.a = math.exp(-1 / (0.05 * SR / 3))
        self.zi = np.array([0.0])
        self.pending = np.zeros((self.LOOK, 2))
        self.comp = Compressor()

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.pending, self.comp.process(x)])
        req = np.minimum(1.0, self.CEIL / np.maximum(np.abs(x).max(axis=1), 1e-9))
        look = self._minf(req, size=self.LOOK + 1, origin=-(self.LOOK // 2), mode="nearest")
        red, self.zi = signal.lfilter([1 - self.a], [1, -self.a], 1 - look, zi=self.zi)
        g = np.minimum(look, 1 - red)
        n = len(x) - self.LOOK
        self.pending = x[n:]
        return x[:n] * g[:n, None]

    def flush(self) -> np.ndarray:
        y = np.clip(self.pending, -self.CEIL, self.CEIL)
        self.pending = np.zeros((0, 2))
        return y


# ---------------------------------------------------------------- 書き出しの実行
class RenderJob:
    def __init__(self, items: list[dict], settings: dict, out_path, fmt: str, tracklist: bool, db_path=None, engine: str = "python"):
        self.items, self.settings, self.out_path, self.fmt, self.want_tl = items, settings, out_path, fmt, tracklist
        self.db_path = db_path
        self.engine = engine                 # 'browser'（画面のストレッチで計算。通常） / 'python'（予備・試験用）
        self.segments: list[dict] = []
        self.audio: dict[int, Path] = {}
        self.audio_event = threading.Event()
        self.tmp: Path | None = None
        self.stage = "prepare"
        self.progress, self.message, self.error, self.result = 0.0, "準備しています…", None, None
        self.cancel = threading.Event()
        self.t_start = time.time()
        self.plan: Plan | None = None
        self.thread = threading.Thread(target=self._run, name="render", daemon=True)

    def status(self) -> dict:
        el = time.time() - self.t_start
        eta = el / self.progress * (1 - self.progress) if 0.02 < self.progress < 1 else None
        return {"progress": round(self.progress, 4), "message": self.message, "error": self.error, "done": self.result is not None,
                "result": self.result, "eta_sec": round(eta) if eta else None, "stage": self.stage,
                "segments": len(self.segments), "segments_done": len(self.audio)}

    # ---- 画面のストレッチで計算した音を受け取る ----
    def segment_info(self) -> list[dict]:
        """画面に渡す区間（刻みごとの入力位置つき）。"""
        plan = self.plan
        out = []
        for g in self.segments:
            info = {**g, "step": STEP, "sr": SR, "pos": [round(float(v), 5) for v in plan.pos[g["deck"]][g["s0"]:g["s1"]]],
                    "done": g["id"] in self.audio, "out_channels": 2}
            if g["kind"] == "stems":           # パートごとの音量（刻みごと。画面で 2ch に混ぜてから送ってもらう＝送る量が 1/4）
                info["pg"] = [[round(float(v), 4) for v in row] for row in plan.pg[g["deck"]][g["s0"]:g["s1"]]]
            out.append(info)
        return out

    def put_audio(self, seg_id: int, data: bytes) -> None:
        g = self.segments[seg_id]
        n = (g["s1"] - g["s0"]) * STEP
        arr = np.frombuffer(data, dtype="<f4")
        if arr.size != 2 * n:                    # パートの音も画面で 2ch に混ぜてある
            raise ValueError(f"区間 {seg_id} の長さが違います（{arr.size} / {2 * n}）")
        p = self.tmp / f"seg{seg_id}.npy"
        np.save(p, arr.reshape(2, n))
        self.audio[seg_id] = p
        self.audio_event.set()

    def _wait_audio(self) -> bool:
        """画面が全区間を計算し終えるのを待つ。60 秒進まなければ諦める（予備の方式で書き出す）。"""
        last, t_last = -1, time.time()
        while len(self.audio) < len(self.segments):
            if self.cancel.is_set():
                raise RuntimeError("中止しました")
            if len(self.audio) != last:
                last, t_last = len(self.audio), time.time()
            elif time.time() - t_last > 60:
                return False
            self.message = f"再生と同じエンジンで計算しています（{len(self.audio)} / {len(self.segments)}）…"
            self.progress = 0.6 * len(self.audio) / max(1, len(self.segments))
            self.audio_event.wait(0.5)
            self.audio_event.clear()
        return True

    def load_tracks(self) -> list[Track]:
        c = db.connect(self.db_path)
        out = []
        for k, it in enumerate(self.items):
            r = c.execute("SELECT id, path, title, artist, album FROM tracks WHERE id = ?", (it["track_id"],)).fetchone()
            a = cache.read(it["track_id"]) if r else None
            if a is None:
                raise ValueError(f"キューの {k + 1} 曲目が解析されていません")
            a = edits.apply(it["track_id"], a, self.db_path)
            t0 = it.get("use_start") if it.get("use_start") is not None else 0.0
            t1 = it.get("use_end") if it.get("use_end") is not None else a["duration"]
            from ..analysis import cutpoints
            cp = cutpoints.for_track(it["track_id"]) or {}
            out.append(Track(k, {**it, "path": r["path"]}, a, BeatMap(a), float(t0), float(t1), float(a.get("lufs", -14)),
                             r["title"] or "", r["artist"] or "", album=r["album"] or "", cut_bars=cp.get("bars") or []))
        return out

    def _run(self):
        sink = None
        try:
            tracks = self.load_tracks()
            # パートごとに鳴らす繋ぎ（Phase 5 B2）: 分離した音が無い曲は、ここで分離して保存する（1 曲 20 秒ほど）
            from ..analysis import stems as stems_mod
            need = sorted(parts_tracks(tracks))
            for n, k in enumerate(need):
                t = tracks[k]
                tid = int(t.item["track_id"])
                if not stems_mod.audio_ready(tid):
                    self.message = f"パートの音を用意しています（{n + 1} / {len(need)}）…"
                    try:
                        stems_mod.compute_audio_for_track(tid, t.item["path"])
                    except Exception as e:                  # 分離できなければ、その繋ぎはパートを分けない
                        print("stems failed", tid, e)
                    if self.cancel.is_set():
                        raise RuntimeError("中止しました")
                t.parts_ok = stems_mod.audio_ready(tid)
            self.message = "繋ぎを計算しています…"
            plan = self.plan = build_plan(tracks, self.settings)
            ext, base = None, 0.0
            if self.engine == "browser":
                import tempfile
                from ..config import DATA_DIR
                for old in DATA_DIR.glob("export_*"):          # 前に消せなかった一時フォルダ（落ちた・消せなかった）
                    if old.is_dir() and time.time() - old.stat().st_mtime > 3600:
                        _remove_tmp(old)
                self.tmp = Path(tempfile.mkdtemp(prefix="export_", dir=DATA_DIR))
                self.segments = segments_of(plan, tracks)
                self.stage = "need_audio"
                if self._wait_audio():
                    ext = {g["id"]: {**g, "audio": np.load(self.audio[g["id"]], mmap_mode="r")} for g in self.segments}
                    base = 0.6
                else:
                    log.warning("browser render timed out; falling back to python-stretch")
                self.stage = "mix"
            sink = WavSink(self.out_path, self.fmt, SR)
            mixer = Mixdown(tracks, plan, self.settings, ceil_db=-0.3 if self.fmt == "wav" else -1.0, ext=ext)
            ext = None                        # 参照は mixer だけに（終わったら手放せるように）
            for done in mixer.run(lambda pcm: sink.write(pcm), self.cancel):
                self.progress = base + done * (0.97 - base)
                self.message = None
            mixer = None                      # 一時ファイル（mmap）を手放す
            self.message = "ファイルにしています…"
            self.result = sink.close(plan.marks if self.want_tl else None)
            sink = None
            self.progress = 1.0
            self.message = "書き出しました"
            self.stage = "done"
        except Exception as e:
            self.error = str(e)[:300] or e.__class__.__name__
            self.stage = "error"
            if sink is not None:
                try:
                    sink.w.close()
                    sink.wav_path.unlink(missing_ok=True)
                except Exception:
                    pass
        finally:
            if self.tmp is not None:          # 画面から受け取った音の一時ファイル
                # mmap で開いたままだと Windows では消せないので、参照を手放してから消す
                ext = mixer = None            # noqa: F841
                import gc
                gc.collect()
                _remove_tmp(self.tmp)


def _remove_tmp(p: Path) -> None:
    import shutil
    for _ in range(10):
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            return
        time.sleep(0.3)
    log.warning("could not remove %s", p)


class Mixdown:
    """操作の表どおりに、各曲をストレッチしてミキサーに通す。split=True なら左＝デッキ0・右＝デッキ1（一致の試験用）。"""

    CH = 176                       # 1 回に計算する刻み数（約 2 秒。MAX_BATCH の倍数にして、まとめる区間が途中で切れないように）

    def __init__(self, tracks: list[Track], plan: Plan, settings: dict, split: bool = False, ceil_db: float = -0.3,
                 ext: dict | None = None):
        self.tracks, self.plan, self.split = tracks, plan, split
        self.ext = ext                       # 画面のストレッチで計算した区間 {id: {deck, kind, s0, s1, audio(ch, n)}}。無ければ python-stretch
        self.target = float(settings.get("lufs_target", -14))
        self.gains = xfade_gains(plan.xf, settings.get("xfade_curve", "smooth"))
        self.ch = [Channel(), Channel()]
        self.echo = [Echo(), Echo()]
        self.lim = Limiter(ceil_db)
        self.st = [None, None]
        self.stem = [None, None]       # パートの音のストレッチ（使う区間の少し前から。Phase 5 B2）

    def _ext_audio(self, d: int, c0: int, c1: int) -> np.ndarray:
        """画面のストレッチで計算した音から、デッキ d の刻み c0..c1 を取り出す（パートの音は pm・pg で切り替えて混ぜる）。"""
        plan = self.plan
        x = np.zeros(((c1 - c0) * STEP, 2), dtype=np.float32)
        for g in self.ext.values():
            if g["deck"] != d or g["kind"] != "main" or g["s1"] <= c0 or g["s0"] >= c1:
                continue
            a, b = max(c0, g["s0"]), min(c1, g["s1"])
            x[(a - c0) * STEP:(b - c0) * STEP] = np.asarray(g["audio"][:, (a - g["s0"]) * STEP:(b - g["s0"]) * STEP]).T
        for g in self.ext.values():
            if g["deck"] != d or g["kind"] != "stems" or g["s1"] <= c0 or g["s0"] >= c1:
                continue
            a, b = max(c0, g["s0"]), min(c1, g["s1"])
            parts = np.asarray(g["audio"][:, (a - g["s0"]) * STEP:(b - g["s0"]) * STEP]).T     # (k, 2)。パートごとの音量は画面で掛けてある
            w = _per_sample(plan.pm[d][a:b].astype(float))[:, None]
            seg = x[(a - c0) * STEP:(b - c0) * STEP]
            x[(a - c0) * STEP:(b - c0) * STEP] = seg * (1 - w) + parts * w
        return x

    def _deck_audio(self, d: int, c0: int, c1: int) -> np.ndarray:
        if self.ext is not None:
            return self._ext_audio(d, c0, c1)
        import python_stretch as ps
        plan = self.plan
        x = np.zeros(((c1 - c0) * STEP, 2), dtype=np.float32)
        s = c0
        while s < c1:
            idx = int(plan.track[d][s])
            if idx < 0:
                s += 1
                continue
            if self.st[d] is None or self.st[d]["idx"] != idx:
                audio = decode(self.tracks[idx].item["path"]).T.astype(np.float32).copy()
                sx = ps.Signalsmith.Stretch()
                sx.preset(2, SR)
                if self.tracks[idx].shift:
                    sx.setTransposeSemitones(self.tracks[idx].shift)   # キー合わせ（再生の keyShift と同じ）
                self.st[d] = {"idx": idx, "audio": audio, "stretch": sx, "ip": int(round(plan.pos[d][s] * SR))}
            S = self.st[d]
            # 16 刻み（8192 サンプル）ずつまとめてストレッチする。python-stretch は 1 回に渡す長さが短いと出力が小さくなる
            # （512 サンプルずつで −4dB、8192 で −0.2dB。P3-8 で、速度が変わり続ける 2 曲目だけ録音より 2dB 小さかったことで判明）。
            # まとめた区間の中は速度を一定とみなすが、区間の境目の位置は操作の表どおり（ずれは溜まらない）
            L = 1
            while (s + L) % MAX_BATCH and s + L < c1 and plan.track[d][s + L] == idx:     # 区切りを 16 刻みごとにそろえる
                L += 1
            pos = plan.pos[d]
            if s + L < plan.n and plan.track[d][s + L] == idx:
                nxt = pos[s + L]
            else:
                nxt = pos[s + L - 1] + (pos[s + L - 1] - pos[s + L - 2] if s + L - 2 >= 0 else DT)
            n_out = L * STEP
            n_in = int(round(nxt * SR)) - S["ip"]
            m = max(1, min(n_in, S["audio"].shape[1] - S["ip"]))
            if S["ip"] < S["audio"].shape[1]:
                S["stretch"].setTimeFactor(m / n_out)
                # 必ず連続したメモリの配列を渡す。python-stretch は (2, n) の列の切り出し（飛び飛びの配列）を正しく読めず、
                # 音が崩れる（P3-8 の試験で、原曲との相関が 0.6 に落ちたことで判明。連続にすると 0.998）
                y = S["stretch"].process(np.ascontiguousarray(S["audio"][:, S["ip"]:S["ip"] + m]))
                k = min(n_out, y.shape[1])
                main = y[:, :k].T * STRETCH_GAIN
                # パートの音（Phase 5 B2）: 使う区間の CH 刻み前から、元の音と同じ速度でストレッチしておき、pm に従って切り替える
                pm = plan.pm[d]
                ahead = pm[s:min(plan.n, s + L + self.CH)]
                if (ahead > 0).any() or self.stem[d] is not None:
                    main = self._mix_parts(d, idx, S["ip"], m, n_out, s, L, main, k)
                x[(s - c0) * STEP:(s - c0) * STEP + k] = main
                S["ip"] += m
            s += L
        return x

    def _mix_parts(self, d: int, idx: int, ip: int, m: int, n_out: int, s: int, L: int, main: np.ndarray, k: int) -> np.ndarray:
        """元の音 main (k, 2) と、同じ区間のパートの音（パートごとの音量を掛けて足したもの）を pm で切り替える。"""
        import python_stretch as ps
        import soundfile as sf
        from ..analysis import stems as stems_mod
        plan = self.plan
        P = self.stem[d]
        if P is None or P["idx"] != idx:
            tid = int(self.tracks[idx].item["track_id"])
            if not self.tracks[idx].parts_ok:
                return main
            sx = ps.Signalsmith.Stretch()
            sx.preset(8, SR)
            if self.tracks[idx].shift:
                sx.setTransposeSemitones(self.tracks[idx].shift)
            P = self.stem[d] = {"idx": idx, "file": sf.SoundFile(stems_mod.audio_path(tid)), "stretch": sx}
        f = P["file"]
        f.seek(min(ip, f.frames))
        seg = f.read(m, dtype="float32", always_2d=True)
        if len(seg) < m:
            seg = np.vstack([seg, np.zeros((m - len(seg), 8), dtype=np.float32)])
        P["stretch"].setTimeFactor(m / n_out)
        y = P["stretch"].process(np.ascontiguousarray(seg.T))[:, :k] * STRETCH_GAIN
        g = np.stack([_per_sample(plan.pg[d][s:s + L, i]) for i in range(4)])[:, :k]       # (4, k)
        parts = sum(y[2 * i:2 * i + 2] * g[i] for i in range(4)).T                        # (k, 2)
        w = _per_sample(plan.pm[d][s:s + L].astype(float))[:k, None]
        out = main * (1 - w) + parts * w
        if not (plan.pm[d][s:min(plan.n, s + L + self.CH)] > 0).any() and (plan.pm[d][max(0, s - 1):s + L] == 0).all():
            P["file"].close()
            self.stem[d] = None                       # 使い終わった
        return out

    def run(self, write, cancel: threading.Event | None = None):
        plan = self.plan
        for c0 in range(0, plan.n, self.CH):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("中止しました")
            c1 = min(plan.n, c0 + self.CH)
            n = (c1 - c0) * STEP
            mix = np.zeros((n, 2))
            for d in range(2):
                tr_idx = plan.track[d][c0:c1]
                if (tr_idx < 0).all():
                    e = self.echo[d].process(np.zeros((n, 2)), plan.echo_delay[d][c0:c1])     # 止まっていても余韻は鳴る
                    mix += e if not self.split else self._side(e, d)
                    if self.st[d] is not None:
                        self.st[d] = None                                                   # 曲が終わったデッキは音声を手放す
                    if self.stem[d] is not None:
                        self.stem[d]["file"].close()
                        self.stem[d] = None
                    continue
                x = self._deck_audio(d, c0, c1)
                lufs = self.tracks[int(tr_idx[tr_idx >= 0][0])].lufs
                trim = 10 ** (max(-12.0, min(9.0, self.target - lufs)) / 20)
                y = self.ch[d].process(x.astype(np.float64), trim, plan.low[d][c0:c1], plan.hp[d][c0:c1])
                jg = plan.jg[d][c0:c1]
                if (jg != 1).any():
                    y = y * _per_sample(jg)[:, None]
                e = self.echo[d].process(y * _per_sample(plan.echo[d][c0:c1])[:, None], plan.echo_delay[d][c0:c1])
                if self.split:        # 試験用: 音量の自動の動きを掛けずに、そのデッキの音を片側へ
                    mix += self._side(y, d)
                else:
                    mix += y * self._gain(d, c0, c1)[:, None] + e
            out = mix if self.split else self.lim.process(mix)
            write((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())
            yield c1 / plan.n
        if not self.split:
            write((np.clip(self.lim.flush(), -1, 1) * 32767).astype("<i2").tobytes())

    CUT_FADE = 88                  # カットインの切り替えの長さ（サンプル。約 2ms。再生のクロスフェーダーの予約と同じくらい）

    def _gain(self, d: int, c0: int, c1: int) -> np.ndarray:
        """デッキ d のクロスフェーダーの倍率（サンプルごと）。次の曲を前の曲の終わりで入れた刻みは、刻みの中の f の位置で
        前の曲を消し、次の曲を鳴らす（刻みの間の直線のままだと 11.6ms のうちに混ざる。design_cutin.md ①）。"""
        g = _per_sample(self.gains[d][c0:c1])
        n = self.plan.n
        for c in self.plan.cuts:
            k = c["k"]
            if not c0 <= k < c1:
                continue
            a = (k - c0) * STEP
            w = np.clip((np.arange(STEP) - c["f"] * STEP) / self.CUT_FADE, 0.0, 1.0)
            if d == c["out"]:
                g[a:a + STEP] = (1 - w) * self.gains[d][k]
            else:
                g[a:a + STEP] = w * self.gains[d][min(n - 1, k + 1)]
        return g

    @staticmethod
    def _side(y: np.ndarray, d: int) -> np.ndarray:
        m = y.mean(axis=1)
        out = np.zeros_like(y)
        out[:, d] = m
        return out


_job: RenderJob | None = None


def start(items, settings, out_path, fmt, tracklist, db_path=None, engine: str = "python") -> RenderJob:
    global _job
    if _job is not None and _job.thread.is_alive():
        raise ValueError("書き出し中です。終わってから始めてください")
    _job = RenderJob(items, settings, out_path, fmt, tracklist, db_path, engine)
    _job.thread.start()
    return _job


def current() -> RenderJob | None:
    return _job
