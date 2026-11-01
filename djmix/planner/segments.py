"""1 曲の使う範囲の候補（使ってみての改善 I-7。おまかせの「1 曲あたりの長さ」「使う区間」と、結果の曲ごとの「区間」）。

おまかせの要約（desc）は「区間の区切りで決めた範囲（おまかせ）」1 つだけを持つ。長さ・位置を指定されたら、解析キャッシュから
その範囲と、繋ぐ部分（始まり・終わりの 16 小節）の響き・拍・ボーカルを計算し直し、要約の use / in / out / vocals を差し替えて使う。
- pos: 'auto'（区間の区切りで決めた範囲。長さを指定したらその長さに合わせる）/ 'head'（頭から）/ 'peak'（盛り上がり中心）/
  'tail'（終わりまで）/ 'full'（曲全体。拍の取れない頭と終わりは除く）
- 範囲の端は区間の区切り（無ければ 4 小節の区切り）にそろえ、繋ぐ部分は拍の取れる所へ（desc.stable_bounds）
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np

from ..analysis import cache, sections
from ..analysis.features import BAR_COLUMNS
from . import desc as desc_mod

POSITIONS = {"auto": "おまかせ", "intro": "冒頭（0:00）から", "head": "前奏を飛ばして頭から", "peak": "盛り上がり中心",
             "tail": "終わりまで", "full": "曲全体（0:00〜最後）"}
FIRST_FROM_TOP_SEC = 30     # ミックスの最初の曲は、飛ばす前奏がこれ以下なら冒頭（0:00）から鳴らす
MIN_SEC = 60
SHORT_SEC = 90              # これ以下の長さは「短くたくさん繋ぐ」用（2026-10-25）: 盛り上がりの所を使い、範囲の下限も長さに合わせて下げる
SHORT_MIN_SEC = 20


def is_short(length: float | None) -> bool:
    return bool(length) and float(length) <= SHORT_SEC


def _len_label(sec: float) -> str:
    return f"約{round(sec)}秒" if sec < 90 else f"約{round(sec / 60, 1)}分"

_memo: OrderedDict = OrderedDict()           # track_id → (a, feats, secs, bt, stems)
_memo_lock = threading.Lock()
_MEMO_MAX = 2000


def _load(tid: int):
    with _memo_lock:
        if tid in _memo:
            _memo.move_to_end(tid)
            return _memo[tid]
    a = cache.read(tid)
    feats = cache.bars_array(tid, len(BAR_COLUMNS))
    if a is None or feats is None or len(feats) != len(a.get("bars", [])) or len(feats) < 16:
        return None
    from ..analysis import stems as stems_mod
    st = stems_mod.read(tid)
    v = (a, feats, sections.compute(a, feats), [b["time"] for b in a["bars"]] + [a["duration"]],
         st if st is not None and len(st) == len(a["bars"]) else None)
    with _memo_lock:
        _memo[tid] = v
        while len(_memo) > _MEMO_MAX:
            _memo.popitem(last=False)
    return v


def _full(a: dict, secs: list[dict], bt: list[float]) -> tuple[int, int]:
    """曲全体（拍の取れない頭・終わりの区間は除く）。"""
    n = len(bt) - 1
    ok = [s for s in secs if "拍なし" not in s["tags"]] or secs
    return ok[0]["bar0"], min(n, ok[-1]["bar1"])


def _snap(bt: list[float], cuts: list[int], t: float, tol: float, lo: int, hi: int, phrase_from: int) -> int:
    """時刻 t に近い区切り（区間の区切りが tol 秒以内に無ければ、phrase_from から 4 小節ごとの区切り）の小節番号。"""
    near = [c for c in cuts if lo <= c <= hi and abs(bt[c] - t) <= tol]
    if near:
        return min(near, key=lambda c: abs(bt[c] - t))
    k = int(np.searchsorted(bt, t))
    k = max(lo, min(hi, k))
    k = phrase_from + round((k - phrase_from) / 4) * 4
    return max(lo, min(hi, k))


def _bar(bt: list[float], t: float) -> int:
    return int(min(range(len(bt)), key=lambda k: abs(bt[k] - t)))


def bounds(tid: int, base_use: dict, length: float | None, pos: str, t0: float | None = None,
           t1: float | None = None) -> tuple[dict, tuple] | None:
    """使う範囲 {t0, t1, bar0, bar1, notes} と読み込んだもの。作れなければ None（要約の範囲のまま）。
    pos='custom' は t0〜t1（秒）を小節にそろえて使う（冒頭 0:00・曲の最後はそのまま）。"""
    L = _load(tid)
    if L is None:
        return None
    a, feats, secs, bt, _ = L
    n = len(bt) - 1
    f0, f1 = _full(a, secs, bt)
    cuts = sorted({s["bar0"] for s in secs} | {f0, f1})
    notes: list[str] = []
    if pos == "full":                       # 本当に曲全体（拍の取れない頭・終わりも含める）
        return {"t0": 0.0, "t1": round(bt[n], 3), "bar0": 0, "bar1": n, "notes": ["曲全体（0:00〜最後）"]}, L
    if pos == "custom":                     # 自分で選んだ区間（そのまま使う。拍の取れる所へはずらさない）
        s0 = max(0.0, float(t0 or 0.0))
        s1 = min(bt[n], float(t1 if t1 is not None else bt[n]))
        b0 = 0 if s0 <= bt[0] + 0.3 else _bar(bt, s0)
        b1 = n if s1 >= bt[n] - 0.3 else _bar(bt, s1)
        if b1 - b0 < 4 or bt[b1] - (0.0 if b0 == 0 and s0 <= bt[0] + 0.3 else bt[b0]) < 20:
            return None
        u0 = 0.0 if s0 <= bt[0] + 0.3 else round(bt[b0], 3)
        return {"t0": u0, "t1": round(bt[b1], 3), "bar0": int(b0), "bar1": int(b1), "notes": ["自分で選んだ区間"]}, L
    if pos == "intro":                      # 冒頭（0:00）から: 終わりだけ長さに合わせて拍の取れる所へ
        want = float(length) if length else float(base_use["t1"] - base_use["t0"])
        b1 = _snap(bt, cuts, want, max(20.0, 0.15 * want) if not is_short(want) else max(4.0, 0.25 * want), 8, f1, 0)
        b0, b1 = desc_mod.stable_bounds(a, bt, 0, b1, notes)
        b0 = 0
        notes.insert(0, f"冒頭から{_len_label(bt[b1])}")
        return {"t0": 0.0, "t1": round(bt[b1], 3), "bar0": 0, "bar1": int(b1), "notes": list(dict.fromkeys(notes))}, L
    else:
        want = float(length) if length else float(base_use["t1"] - base_use["t0"])
        want = min(want, bt[f1] - bt[f0])
        # 区間の区切りへ吸着する幅。短いときは長さの 1/4 まで（30 秒の曲が 50 秒にならないように）
        tol = max(20.0, 0.15 * want) if not is_short(want) else max(4.0, 0.25 * want)
        if pos == "auto" and is_short(want):
            pos = "peak"                    # 短く使うときは、曲のいちばん聴かせ所（盛り上がり）を使う
        if pos == "tail":
            b1 = f1
            b0 = _snap(bt, cuts, bt[b1] - want, tol, f0, b1 - 8, b1)
            label = "終わりまで{}"
        elif pos == "peak":
            rms = feats[:, 0]
            span = max(8, int(np.searchsorted(bt, bt[f0] + want) - f0))
            best, bs = -1e9, f0
            for s in range(f0, max(f0 + 1, f1 - span + 1)):
                m = float(rms[s:s + span].mean())
                if m > best:
                    best, bs = m, s
            b0 = _snap(bt, cuts, bt[bs], tol, f0, f1 - 8, bs)
            b1 = _snap(bt, cuts, bt[b0] + want, tol, b0 + 8, f1, b0)
            label = "盛り上がり中心に{}"
        else:                               # head / auto
            b0 = base_use["bar0"] if pos == "auto" else f0
            b1 = _snap(bt, cuts, bt[b0] + want, tol, b0 + 8, f1, b0)
            label = ("頭から" if pos == "head" else "") + "{}"
    if bt[b1] - bt[b0] < (min(MIN_SEC, max(SHORT_MIN_SEC, 0.6 * want)) if is_short(want) else MIN_SEC):
        return None
    b0, b1 = desc_mod.stable_bounds(a, bt, b0, b1, notes)
    if pos != "full":                       # 長さの説明は、拍の取れる所へずらした後の長さで
        notes.insert(0, label.format(_len_label(bt[b1] - bt[b0])))
    return {"t0": round(bt[b0], 3), "t1": round(bt[b1], 3), "bar0": int(b0), "bar1": int(b1), "notes": list(dict.fromkeys(notes))}, L


def variant(tid: int, base: dict, length: float | None, pos: str, t0: float | None = None, t1: float | None = None) -> dict | None:
    """要約 base の use / in / out / vocals を、長さ length（秒。None ならおまかせ）・位置 pos の範囲に差し替える値。"""
    if not length and pos in ("auto", None):
        return None
    r = bounds(tid, base["use"], length, pos or "auto", t0, t1)
    if r is None:
        return None
    use, (a, feats, _, _, st) = r
    seg = {"len": length, "pos": pos or "auto"}
    if pos == "custom":
        seg.update(t0=use["t0"], t1=use["t1"])
    return {"use": use, "in": desc_mod._region(a, feats, use, "in"), "out": desc_mod._region(a, feats, use, "out"),
            "vocals": [round(float(v), 2) for v in st[use["bar0"]:use["bar1"], 0]] if st is not None else None, "seg": seg}


def choices(tid: int, base: dict, length: float | None) -> list[dict]:
    """結果の曲ごとの「区間」の候補（おまかせ・頭から・盛り上がり中心・終わりまで・曲全体）。同じ範囲になるものはまとめる。"""
    out, seen = [], set()
    for pos, label in POSITIONS.items():
        if pos == "auto" and not length:
            use = base["use"]
        else:
            r = bounds(tid, base["use"], length, pos)
            if r is None:
                continue
            use = r[0]
        key = (round(use["t0"], 1), round(use["t1"], 1))
        if key in seen:
            continue
        seen.add(key)
        out.append({"pos": pos, "len": length, "label": label, "use": use})
    return out
