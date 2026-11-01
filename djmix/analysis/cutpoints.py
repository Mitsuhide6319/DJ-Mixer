"""カットインの切り替えポイントの候補と、音量合わせ（2026-10-03。design_cutin.md）。

カットイン = 前の曲を小節の頭でスパッと止め、同じ瞬間に次の曲を小節の頭（1 拍目）から鳴らす繋ぎ。重ねないので、テンポの違う曲どうしでも繋げる。
DJ がカットインで気をつけること（ユーザーの指定）:
  ① CUE でリズムを取ってタイミングを合わせる → 再生側で、前の曲の小節の頭の時刻に次の曲の 1 拍目をサンプル単位で予約する（runner.js / render.py）
  ② 曲がキレイに途切れるポイント（サビの切れ目など）を把握しておく → ここで「切り替えポイント」の候補に点数を付ける
  ③ TRIM で音量を揃えておく → ここで、切り替える前後の数小節の音量の差から、次の曲の入りに掛ける音量（dB）を求める

候補の点数（前の曲の「ここで切る」= 小節 b の頭。直前の小節 b-1 までが鳴る）:
  区間の切れ目（sections.py）+3 ／ 区間の頭から 8 小節の区切り +1.5（4 小節 +0.5）／ 直前 4 小節が曲の中で大きい（サビの終わり候補）+1
  ／ 直前の小節に歌が無い +1、歌が小節をまたいで続く −2 ／ 拍の取れない所 −3
次の曲の「ここから入る」= 小節 b の頭:
  区間の頭 +3 ／ 曲の最初の小節 +2 ／ その後 4 小節が大きい（サビ頭から入る）+1.5 ／ 歌い出し +0.5 ／ 8 小節の区切り +1（4 小節 +0.5）
時刻は解析時の小節。ユーザーが拍子を直していたら、画面側で今の小節に吸着させる（sections と同じ）。
"""
from __future__ import annotations

import math
import threading

import numpy as np

from . import cache, sections
from .features import BAR_COLUMNS

TRIM_MAX_DB = 6.0          # 自動の音量合わせは ±6dB まで
TRIM_DEAD_DB = 1.0         # 差がこれ未満なら合わせない（聞き分けにくい）
LEVEL_BARS = 4             # 切り替えの前後、何小節の音量を比べるか
TRIM_BACK_BARS = 16        # 合わせた音量を、次の曲の何小節かけて元（曲ごとの音量の揃え）へ戻すか
GOOD = 3.0                 # 画面で「おすすめ」とする点数

_memo: dict[tuple[int, int], dict] = {}
_lock = threading.Lock()


def _db(x: float) -> float:
    return 20 * math.log10(max(float(x), 1e-6))


def compute(a: dict, feats: np.ndarray, segs: list[dict], vocals: np.ndarray | None) -> dict:
    bars = a["bars"]
    n = len(bars)
    times = [float(b["time"]) for b in bars]
    rms = feats[:, 0] if len(feats) == n else np.zeros(n)
    bar_db = [round(_db(v), 2) for v in rms]
    if n < 8:
        return {"bars": [{"t": t, "db": d} for t, d in zip(times, bar_db)], "outs": [], "ins": []}
    med = float(np.median(bar_db))
    starts = sorted({int(s["bar0"]) for s in segs if 0 < int(s["bar0"]) < n})
    sec_of = lambda b: max([0] + [s for s in starts if s <= b])        # noqa: E731  小節 b を含む区間の頭
    from .stems import VOCAL_ON
    voc = vocals[:, 0] if vocals is not None and len(vocals) == n else None
    if voc is not None and (voc >= VOCAL_ON).sum() < 4:
        voc = None                        # 歌の無い曲（インスト）は歌の切れ目を見ない

    def nb(b0, b1):        # 小節 b0..b1-1 に拍の取れない所があるか
        t0, t1 = times[max(0, b0)], times[b1] if b1 < n else a["duration"]
        return any(min(t1, e) - max(t0, s) > 0.2 for s, e in a.get("no_beat") or [])

    def lvl(b0, b1):
        seg = rms[max(0, b0):max(0, min(n, b1))]
        return _db(np.sqrt(np.mean(seg ** 2))) if len(seg) else -120.0

    outs, ins = [], []
    for b in range(4, n):
        sc, tags = 0.0, []
        if b in starts:
            sc += 3; tags.append("区間の切れ目")
        ph = b - sec_of(b)
        if ph and ph % 8 == 0:
            sc += 1.5; tags.append("8 小節の区切り")
        elif ph and ph % 4 == 0:
            sc += 0.5
        before, after = lvl(b - 4, b), lvl(b, b + 4)
        if before > med + 1.5 and after < before - 1:
            sc += 1; tags.append("盛り上がりの終わり")
        if voc is not None:
            if voc[b - 1] < VOCAL_ON:
                sc += 1; tags.append("歌の切れ目")
            elif b < n and voc[b] >= VOCAL_ON:
                sc -= 2; tags.append("歌の途中")
        if nb(b - 2, min(n, b + 1)):
            sc -= 3; tags.append("拍なし")
        outs.append({"bar": b, "t": round(times[b], 3), "score": round(sc, 2), "tags": tags})
    for b in range(0, n - 8):
        sc, tags = 0.0, []
        if b == 0:
            sc += 2; tags.append("曲の頭")
        elif b in starts:
            sc += 3; tags.append("区間の頭")
        ph = b - sec_of(b)
        if ph and ph % 8 == 0:
            sc += 1; tags.append("8 小節の区切り")
        elif ph and ph % 4 == 0:
            sc += 0.5
        if lvl(b, b + 4) > med + 1.5 and lvl(b - 4, b) < lvl(b, b + 4) - 1:
            sc += 1.5; tags.append("サビ頭の候補")
        if voc is not None and voc[b] >= VOCAL_ON and (b == 0 or voc[b - 1] < VOCAL_ON):
            sc += 0.5; tags.append("歌い出し")
        if nb(b, b + 2):
            sc -= 3; tags.append("拍なし")
        ins.append({"bar": b, "t": round(times[b], 3), "score": round(sc, 2), "tags": tags})
    return {"bars": [{"t": t, "db": d} for t, d in zip(times, bar_db)], "outs": outs, "ins": ins}


def for_track(track_id: int) -> dict | None:
    """解析済みなら候補を返す（同じ解析結果には計算結果を使い回す）。"""
    a = cache.read(track_id)
    if a is None:
        return None
    from . import stems
    voc = stems.read(track_id)
    key = (track_id, cache.ANALYSIS_VERSION, voc is not None)
    with _lock:
        if key in _memo:
            return _memo[key]
    feats = cache.bars_array(track_id, len(BAR_COLUMNS))
    res = compute(a, feats if feats is not None else np.zeros((0, len(BAR_COLUMNS))), sections.for_track(track_id) or [], voc)
    with _lock:
        _memo[key] = res
        if len(_memo) > 256:
            _memo.pop(next(iter(_memo)))
    return res


def forget(track_id: int) -> None:
    with _lock:
        for k in [k for k in _memo if k[0] == track_id]:
            del _memo[k]


# ---------------------------------------------------------------- 音量合わせ（transition.js の levelDb / cutTrimDb と同じ式）
def level_db(bars: list[dict], t0: float, t1: float) -> float | None:
    """[t0, t1) に頭がある小節の音量（RMS の二乗平均。dB）。小節が無ければ None。"""
    v = [10 ** (b["db"] / 10) for b in bars if t0 - 1e-3 <= b["t"] < t1 - 1e-3]
    return 10 * math.log10(sum(v) / len(v)) if v else None


def loud_db(lufs: float, target: float) -> float:
    """曲ごとの音量の揃え（mixer.js の setLoudness と同じ。±の上限つき）。"""
    return max(-12.0, min(9.0, target - lufs))


def auto_trim_db(bars_out: list[dict], t_cut: float, bar_out: float, lufs_out: float,
                 bars_in: list[dict], t_in: float, bar_in: float, lufs_in: float, target: float) -> float:
    """次の曲の入りに掛ける音量（dB）: 前の曲の最後の LEVEL_BARS 小節と、次の曲の最初の LEVEL_BARS 小節の聞こえる大きさをそろえる。"""
    lo = level_db(bars_out, t_cut - LEVEL_BARS * bar_out, t_cut)
    li = level_db(bars_in, t_in, t_in + LEVEL_BARS * bar_in)
    if lo is None or li is None:
        return 0.0
    d = (lo + loud_db(lufs_out, target)) - (li + loud_db(lufs_in, target))
    if abs(d) < TRIM_DEAD_DB:
        return 0.0
    return round(max(-TRIM_MAX_DB, min(TRIM_MAX_DB, d)), 1)


def trim_db(params: dict, auto: float) -> float:
    """繋ぎの設定 cutTrim（'auto' / 'off' / dB の数）から、実際に掛ける dB。"""
    v = params.get("cutTrim", "auto")
    if v == "off":
        return 0.0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return max(-TRIM_MAX_DB, min(TRIM_MAX_DB, float(v)))
    return auto
