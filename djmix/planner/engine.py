"""相性と並びの探索（design_phase3.md §4）。試作 work/phase3/planner_proto.py を本体にしたもの。

候補の曲どうしの相性を行列でまとめて計算し（numpy）、ビームサーチはその行列を引くだけにする。
1 万曲の解析済みライブラリでも、候補を 600 曲に絞れば 3 案を数秒で返せる。
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from .. import db
from ..analysis import cache
from ..analysis.features import BAR_COLUMNS
from . import desc as desc_mod
from .genres import group_of

W_TEMPO, W_KEY, W_CHROMA, W_BEAT, W_MOOD = 0.30, 0.20, 0.10, 0.15, 0.25   # 相性の配分（打合せ14・試作）
W_PAIR, W_FLOW = 0.75, 0.25                                                # 並びの評価: 相性と流れ
SAME_ARTIST_PENALTY = 0.15
# アルバムで選ぶ（2026-10-25）: 同じアルバムが続くのを避け、まだ使っていないアルバムを優先する（最後に、入らなかったアルバムの曲を差し込む）
SAME_ALBUM_PENALTY = 0.12
ALBUM_COVER_BONUS = 0.15
MIN_ALBUMS = 3
MAX_ALL_TRACKS = 200
TPL_BARS = {"long": 16, "short": 8, "echo": 2, "cut": 0}
# つなぎ方（design_cutin.md）: 'auto' = おまかせ（拍を合わせられない組み合わせは、切れ目が良ければカットイン）、'mix' = 重ねて繋ぐ、'cut' = カットイン
JOIN_MODES = ("auto", "mix", "cut")
CUT_SEARCH_OUT = (-8, 4)        # カットインの切り替えポイントを、使う範囲の終わりの何小節前〜後から探すか
CUT_SEARCH_IN = (-4, 8)         # 次の曲の入りを、使う範囲の頭の何小節前〜後から探すか
CUT_MIN_USE = 60.0              # 切り替えポイントへ寄せても、使う範囲は 60 秒以上残す
MAX_CANDS = 600
BEAM = 60
FLOWS = {
    "up": lambda x, e0: e0 + (min(1.0, e0 + 0.45) - e0) * x,
    "keep": lambda x, e0: e0,
    "peak": lambda x, e0: e0 + 0.4 * np.sin(np.pi * x),
    "down": lambda x, e0: e0 - (e0 - max(0.0, e0 - 0.45)) * x,
}
FLOW_NAMES = {"up": "上げていく", "keep": "キープ", "peak": "山を作る", "down": "落ち着かせる"}


def _camelot(s: str) -> tuple[int, str]:
    return int(s[:-1]), s[-1]


def _key_rel(k1: str, k2: str) -> tuple[float, str]:
    (n1, l1), (n2, l2) = _camelot(k1), _camelot(k2)
    dn = min((n1 - n2) % 12, (n2 - n1) % 12)
    if dn == 0 and l1 == l2:
        return 1.0, "同じキー"
    if (dn == 1 and l1 == l2) or (dn == 0 and l1 != l2):
        return 0.85, "隣のキー"
    if dn == 2 and l1 == l2:
        return 0.5, "2つ隣のキー"
    return 0.15, "キーが離れている"


CAMELOTS = [f"{n}{l}" for n in range(1, 13) for l in "AB"]
_CI = {c: i for i, c in enumerate(CAMELOTS)}
_REL24 = np.array([[_key_rel(a, b)[0] for b in CAMELOTS] for a in CAMELOTS])
RAMP_MIN, RAMP_MAX = 0.06, 0.15          # テンポランプを使うテンポの差（log。design_phase4.md §2.1）
VOCAL_ON = 0.25                          # ボーカルがある小節（analysis/stems.py と同じ）
W_VOCAL = 0.15                           # ボーカルの重なりの減点（重なる小節の割合 × これ。design_phase4.md §3.2）
VBARS = 16                               # 相性で比べる小節数（繋ぐ部分）


def shift_camelot(c: str, s: int) -> str:
    """Camelot 表記を s 半音ずらす（1 半音で円の上を 7 つ進む。transition.js の shiftCamelot と同じ）。"""
    if not s:
        return c
    n, l = int(c[:-1]), c[-1]
    return f"{((n - 1 + 7 * s) % 12 + 12) % 12 + 1}{l}"


def _z(v: np.ndarray) -> np.ndarray:
    return (v - v.mean()) / (v.std() + 1e-9)


class Pool:
    """解析済みの曲の要約と、曲どうしを比べるための標準化した値（ライブラリ全体で標準化）。"""

    def __init__(self, descs: dict[int, dict]):
        self.d = descs
        self.ids = list(descs)
        if not self.ids:
            self.index = {}
            return
        ex = [descs[i]["extra"] for i in self.ids]
        cen = np.log(np.array([max(1.0, e["centroid"]) for e in ex]))
        low = np.array([e["low_ratio"] for e in ex])
        ons = np.array([e["onset_rate"] for e in ex])
        bpm = np.log(np.array([max(1.0, descs[i]["bpm"]) for i in self.ids]))
        raw = 0.5 * _z(ons) + 0.3 * _z(cen) + 0.2 * _z(bpm)
        energy = 1 / (1 + np.exp(-1.2 * raw))             # 0..1（極端な曲に引っぱられないよう、なだらかに）
        timbre = np.stack([_z(cen), _z(low), _z(ons)], axis=1)
        self.index = {tid: k for k, tid in enumerate(self.ids)}
        for k, tid in enumerate(self.ids):
            descs[tid]["energy"] = float(energy[k])
            descs[tid]["timbre"] = timbre[k]
            descs[tid]["group"] = group_of(descs[tid].get("genre"))


_pool: Pool | None = None
_pool_key = None
_pool_lock = threading.Lock()


def get_pool(db_path=None) -> Pool:
    """要約が増えたら作り直す（まとめて解析の途中でも最新の曲が候補に入る）。"""
    global _pool, _pool_key
    c = db.connect(db_path)
    # 今の版の要約の数も見る（版を上げて作り直している途中は、行の数は変わらずに中身だけ入れ替わるため。Phase 5 F で見つけた）
    key = (str(db_path), c.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0), SUM(version = ?) FROM track_desc",
                                   (desc_mod.DESC_VERSION,)).fetchone()[:])
    with _pool_lock:
        if _pool is None or key != _pool_key:
            _pool, _pool_key = Pool(desc_mod.load_all(db_path)), key
        return _pool


# ---------------------------------------------------------------- 相性の行列
class Matrix:
    """候補の曲 ids の、あらゆる並び (a→b) の相性と内訳。"""

    def __init__(self, pool: Pool, ids: list[int], join_mode: str = "auto"):
        self.pool, self.ids = pool, ids
        self.join_mode = join_mode if join_mode in JOIN_MODES else "auto"
        self.pos = {t: k for k, t in enumerate(ids)}
        D = [pool.d[t] for t in ids]
        n = len(D)
        bpm = np.array([d["bpm"] for d in D])
        # テンポ: 倍/半を考慮して、速度の変化が一番小さい倍率
        best = None
        for k in (1.0, 2.0, 0.5):
            r = k * bpm[:, None] / bpm[None, :]
            if best is None:
                best, kk = r, np.full((n, n), k)
            else:
                better = np.abs(np.log(r)) < np.abs(np.log(best))
                best, kk = np.where(better, r, best), np.where(better, k, kk)
        self.rate, self.k = best, kk
        dlog = np.abs(np.log(best))
        self.ts = np.where(dlog < 0.03, 1.0, np.clip(1 - (dlog - 0.03) / 0.2, 0, 1))
        self.dlog = dlog
        # キー（前の曲の末尾 30 秒と次の曲の冒頭 30 秒）。和音がうすい部分どうしなら影響を弱める
        cams = sorted({d["key_tail"] for d in D} | {d["key_head"] for d in D})
        ci = {c: j for j, c in enumerate(cams)}
        rel = np.array([[_key_rel(a, b)[0] for b in cams] for a in cams])
        tail = np.array([ci[d["key_tail"]] for d in D])
        head = np.array([ci[d["key_head"]] for d in D])
        ks = rel[tail][:, head]
        # キー合わせ（Phase 4）: 次の曲を ±1 半音ずらすと合うなら、少し割り引いてその値を使う（実際のずらし方は並びが決まってから順に決める）
        ti = np.array([_CI[d["key_tail"]] for d in D]); hi_ = np.array([_CI[d["key_head"]] for d in D])
        for s_ in (-1, 1):
            hs = np.array([_CI[shift_camelot(CAMELOTS[h], s_)] for h in hi_])
            ks = np.maximum(ks, 0.95 * _REL24[ti][:, hs])
        harm_o = np.array([d["out"]["harm"] for d in D])
        harm_i = np.array([d["in"]["harm"] for d in D])
        hw = np.minimum(1.0, 3 * np.minimum(harm_o[:, None], harm_i[None, :]))
        self.ks = 1 - hw * (1 - ks)
        # 繋ぐ部分の響き（クロマの近さ）
        co = np.array([d["out"]["chroma"] for d in D]); co /= np.linalg.norm(co, axis=1, keepdims=True) + 1e-9
        cn = np.array([d["in"]["chroma"] for d in D]); cn /= np.linalg.norm(cn, axis=1, keepdims=True) + 1e-9
        self.cos = co @ cn.T
        # 拍を合わせられるか
        nb_o = np.array([d["out"]["nobeat"] for d in D]) < 1
        nb_i = np.array([d["in"]["nobeat"] for d in D]) < 1
        self.beat = nb_o[:, None] & nb_i[None, :] & (dlog < math.log(1.3))
        # テンポランプ（Phase 4）: テンポの差が 6〜15% でも、重なりの間にテンポを移せば無理なく繋げる
        self.ramp = self.beat & (dlog >= RAMP_MIN) & (dlog <= RAMP_MAX)
        self.ts = np.where(self.ramp, np.maximum(self.ts, 0.8), self.ts)
        # 音の雰囲気の近さ
        tim = np.stack([d["timbre"] for d in D])
        dist = np.linalg.norm(tim[:, None, :] - tim[None, :, :], axis=2)
        self.mood = np.clip(1 - dist / 2.5, 0, 1)
        # ボーカルの重なり（Phase 4 B1）: 前の曲の最後の 16 小節と次の曲の最初の 16 小節で、両方にボーカルがある小節の割合
        def vmat(side):
            m = np.zeros((n, VBARS))
            for r, d in enumerate(D):
                v = d.get("vocals")
                if v:
                    seg = v[-VBARS:] if side == "out" else v[:VBARS]
                    if side == "out":
                        m[r, VBARS - len(seg):] = np.asarray(seg) > VOCAL_ON
                    else:
                        m[r, :len(seg)] = np.asarray(seg) > VOCAL_ON
            return m
        self.vclash = (vmat("out") @ vmat("in").T) / VBARS
        # エコーアウトになる組み合わせは音が重ならないので、ボーカルの重なりを数えない
        self.blend = self.beat & (self.ramp | (self.ts > 0.4))
        self.vclash = self.vclash * self.blend
        # カットイン（design_cutin.md）は重ねないので、テンポの差・拍の取れなさはあまり効かない。
        # 'cut' はすべての繋ぎで、'auto' は重ねて繋げない組み合わせ（カットインで繋ぐ候補）で、テンポの点を底上げする
        ts_s, beat_s = self.ts, self.beat.astype(float)
        if self.join_mode == "cut":
            ts_s, beat_s = np.maximum(self.ts, 0.7), np.ones_like(beat_s)
        elif self.join_mode == "auto":
            ts_s = np.where(self.blend, self.ts, np.maximum(self.ts, 0.4))
        self.S = (W_TEMPO * ts_s + W_KEY * self.ks + W_CHROMA * self.cos + W_BEAT * beat_s + W_MOOD * self.mood
                  - W_VOCAL * self.vclash)
        np.fill_diagonal(self.S, -1)
        self.energy = np.array([d["energy"] for d in D])
        self.use_len = np.array([d["use"]["t1"] - d["use"]["t0"] for d in D])
        self.use_bars = np.array([d["use"]["bar1"] - d["use"]["bar0"] for d in D])
        # 「1 曲あたりの長さ」で短く（90 秒以下）を選んだ曲。重なりを曲の長さに合わせて短くする（ふつうの長さはループで延ばす。Phase 4）
        from .segments import is_short
        self.short = np.array([is_short((d.get("seg") or {}).get("len")) for d in D])
        self.bar_sec = np.array([4 * 60 / max(1.0, d["bpm"]) for d in D])
        arts = [(d.get("artist") or "").strip().lower() for d in D]
        au = {a: j for j, a in enumerate(sorted(set(arts)))}
        self.artist = np.array([au[a] if a else -1 - j for j, a in enumerate(arts)])

    def tpl(self, i: int, j: int) -> str:
        if not self.beat[i, j]:
            return "echo"
        if self.ramp[i, j]:
            return "long"                       # テンポランプは長く重ねてテンポを移す
        if self.ts[i, j] > 0.8 and self.ks[i, j] > 0.6:
            return "long"
        return "short" if self.ts[i, j] > 0.4 else "echo"

    def join(self, a: int, b: int) -> dict:
        """a→b（曲 id）の繋ぎ方・相性・内訳・理由。"""
        i, j = self.pos[a], self.pos[b]
        da, db_ = self.pool.d[a], self.pool.d[b]
        tpl = self.tpl(i, j)
        rate, k = float(self.rate[i, j]), float(self.k[i, j])
        kname = _key_rel(da["key_tail"], db_["key_head"])[1]
        why = [f"テンポ {da['bpm']:.0f}→{db_['bpm']:.0f}（{(rate - 1) * 100:+.1f}%{'・×' + ('2' if k == 2 else '½') if k != 1 else ''}）",
               f"キー {da['key_tail']}→{db_['key_head']} {kname}" + ("（和音がうすく影響小）" if self.ks[i, j] > 0.6 and _key_rel(da['key_tail'], db_['key_head'])[0] < 0.85 else "")]
        if not self.beat[i, j]:
            why.append("拍を合わせられないのでエコーアウト")
        if self.ramp[i, j]:
            why.append("テンポランプで繋ぐ（重なりの間にテンポを移す）")
        if self.mood[i, j] < 0.35:
            why.append("音の雰囲気が違う")
        if self.vclash[i, j] > 0:
            why.append(f"ボーカルが重なる所がある（{round(self.vclash[i, j] * VBARS)} 小節）")
        bars = self.ov_bars(i, j, tpl)
        if tpl == "long" and bars < 12:
            tpl = "short"                       # 曲が短くて長く重ねられない
            why.append(f"曲が短いので重なりを {bars} 小節に")
        elif tpl == "short" and bars < TPL_BARS["short"]:
            why.append(f"曲が短いので重なりを {bars} 小節に")
        return {"tpl": tpl, "bars": bars, "score": round(float(self.S[i, j]), 3),
                "params": {"tempo": "ramp" if self.ramp[i, j] else "sync", "loop": 0, "shift": 0},
                "tempo": round(float(self.ts[i, j]), 2), "key": round(float(self.ks[i, j]), 2),
                "chroma": round(float(self.cos[i, j]), 2), "mood": round(float(self.mood[i, j]), 2), "why": why}

    def ov_bars(self, i: int, j: int, tpl: str) -> int:
        """重なりの小節数: 繋ぎ方の標準（16/8/2）を、2 曲の短い方の使う範囲の 1/3 までに抑える（4 小節単位。短くたくさん繋ぐとき。2026-10-25）。"""
        base = TPL_BARS[tpl]
        if tpl in ("echo", "cut") or not (self.short[i] or self.short[j]):
            return base
        cap = int(min(self.use_bars[i], self.use_bars[j]) // 3) // 4 * 4
        return max(4, min(base, cap))

    def overlap_sec(self, i: int, j: int) -> float:
        tpl = self.tpl(i, j)
        return self.ov_bars(i, j, tpl) * self.bar_sec[i]


# ---------------------------------------------------------------- 探索
def _beam(M: Matrix, start: int, stop, flow, target_sec: float, avoid: np.ndarray, allowed: np.ndarray | None = None,
          width: int = BEAM, flow_w: float = W_FLOW, bonus: np.ndarray | None = None, album: np.ndarray | None = None):
    """ビームサーチ。状態 = (合計点, 並び(行列の番号), 長さ秒)。stop(seq, length) が真なら完了。"""
    n = len(M.ids)
    e0 = M.energy[start]
    f = FLOWS[flow] if isinstance(flow, str) else flow      # 自分で描いた流れ（P3-9）は関数で渡す
    beams = [(0.0, [start], float(M.use_len[start]))]
    done = []
    base_mask = np.ones(n, bool) if allowed is None else allowed.copy()
    for _ in range(200):
        cand = []
        for score, seq, length in beams:
            last = seq[-1]
            mask = base_mask.copy()
            mask[seq] = False
            if not mask.any():
                continue
            x = np.minimum(1.0, (length + M.use_len / 2) / max(1.0, target_sec))
            ef = 1 - np.minimum(1.0, np.abs(M.energy - f(x, e0)) / 0.35) if flow_w else 0.0
            tot = W_PAIR * (M.S[last] + (bonus if bonus is not None else 0)) + flow_w * ef - avoid
            recent = {M.artist[s] for s in seq[-2:]}
            if recent:
                tot = tot - SAME_ARTIST_PENALTY * np.isin(M.artist, list(recent))
            if album is not None:          # アルバムで選ぶ: 同じアルバムが続かない・まだ使っていないアルバムを優先
                used = set(album[seq].tolist())
                tot = tot - SAME_ALBUM_PENALTY * (album == album[last]) + ALBUM_COVER_BONUS * x * ~np.isin(album, list(used))
            tot = np.where(mask, tot, -np.inf)
            top = np.argpartition(-tot, min(12, mask.sum() - 1))[:12]
            for j in top:
                if np.isfinite(tot[j]):
                    ov = M.overlap_sec(last, j)
                    cand.append((score + float(tot[j]), seq + [int(j)], length + float(M.use_len[j]) - ov))
        if not cand:
            if not done:
                done = beams              # 候補を使い切っても目標に届かない（アルバムで選ぶなど）: そこまでの並びを使う
            break
        cand.sort(key=lambda b: -b[0] / len(b[1]))
        beams = []
        seen = set()
        for b in cand:
            key = (b[1][-1], frozenset(b[1]))
            if key in seen:
                continue
            seen.add(key)
            (done if stop(b[1], b[2]) else beams).append(b)
            if len(beams) >= width:
                break
        if not beams:
            break
    done.sort(key=lambda b: -b[0] / max(1, len(b[1]) - 1))
    return done


def _pack(M: Matrix, seq: list[int], with_env: bool = True, modes: dict[int, str] | None = None) -> dict:
    ids = [M.ids[k] for k in seq]
    tracks, length = [], 0.0
    for n, (k, tid) in enumerate(zip(seq, ids)):
        d = M.pool.d[tid]
        use = d["use"]
        # ミックスの最初の曲は前から繋ぐ曲が無いので、短い前奏（拍の取れない所・最初の小節頭の前）を飛ばさず冒頭（0:00）から鳴らす
        from .segments import FIRST_FROM_TOP_SEC
        from .segments import is_short
        # 短くたくさん繋ぐとき（1 曲 90 秒以下）は盛り上がりの所を使うので、冒頭からにはしない
        if (n == 0 and (d.get("seg") or {}).get("pos", "auto") in ("auto", "head") and 0 < use["t0"] <= FIRST_FROM_TOP_SEC
                and not is_short((d.get("seg") or {}).get("len"))):
            use = {**use, "t0": 0.0, "notes": [x for x in use.get("notes", []) if "イントロ" not in x and "始める" not in x] + ["最初の曲なので冒頭から"]}
        row = {"id": tid, "title": d.get("title"), "artist": d.get("artist"), "album": d.get("album"), "genre": d.get("genre"), "rating": d.get("rating", 0),
               "bpm": d["bpm"], "key": d["key"], "energy": round(d["energy"], 3), "duration": d["duration"],
               "use": use, "seg": d.get("seg") or {"len": None, "pos": "auto"}}
        if with_env:
            row["env"] = _envelope(tid, use)
        if n + 1 < len(seq):
            row["join"] = M.join(tid, ids[n + 1])
            length += M.use_len[k] - M.overlap_sec(k, seq[n + 1])
        else:
            length += M.use_len[k]
        tracks.append(row)
    _finish_joins(M, tracks, modes)
    pairs = [M.S[a, b] for a, b in zip(seq, seq[1:])]
    return {"score": round(float(np.mean(pairs)), 3) if pairs else 0.0, "minutes": round(length / 60, 1), "tracks": tracks}


def _finish_joins(M: Matrix, tracks: list[dict], modes: dict[int, str] | None = None) -> None:
    """並びが決まった後の繋ぎの仕上げ（Phase 4。design_phase4.md §2.1）:
    キー合わせ — 前の曲のずらし（あれば）を引き継いだ末尾のキーと、次の曲の冒頭のキーが「同じ／隣」でなければ、次の曲を ±1 半音ずらして合うか試す。
    ループ — 前の曲の使う範囲が短い（重なり＋8 小節未満）なら、最後の 4 小節を繰り返して重なりを取る。"""
    shift_out = 0
    for n in range(len(tracks) - 1):
        a, b = M.pool.d[tracks[n]["id"]], M.pool.d[tracks[n + 1]["id"]]
        j = tracks[n]["join"]
        # カットインにするか（つなぎ目ごとの指定 → 全体のつなぎ方 → おまかせ）。カットインはキー合わせ・歌・パート・ループを使わない
        mode = (modes or {}).get(n) or M.join_mode
        if mode == "cut" or (mode == "auto" and j["tpl"] == "echo"):
            if _make_cut(tracks[n], tracks[n + 1], j, forced=mode == "cut"):
                shift_out = 0
                continue
        j["mode"] = "mix"
        tail = shift_camelot(a["key_tail"], shift_out)
        base = _key_rel(tail, b["key_head"])[0]
        best = 0
        if base < 0.85 and j["tpl"] != "echo":
            for s in (1, -1):
                if _key_rel(tail, shift_camelot(b["key_head"], s))[0] >= 0.85:
                    best = s
                    break
        j["params"]["shift"] = best
        if best:
            j["why"] = [w for w in j["why"] if not w.startswith("キー ")] + [
                f"キー {tail}→{shift_camelot(b['key_head'], best)}（次の曲を {best:+d} 半音ずらして合わせる）"]
        elif shift_out:
            j["why"] = [w for w in j["why"] if not w.startswith("キー ")] + [f"キー {tail}→{b['key_head']} {_key_rel(tail, b['key_head'])[1]}"]
        _avoid_vocal_clash(tracks[n], tracks[n + 1], a, b, j)
        _choose_parts(M, a, b, j)
        use_bars = tracks[n]["use"]["bar1"] - tracks[n]["use"]["bar0"]
        from .segments import is_short
        if j["tpl"] in ("long", "short") and use_bars < j["bars"] + 8 and not is_short((tracks[n].get("seg") or {}).get("len")):
            j["params"]["loop"] = 4
            j["why"].append("前の曲の範囲が短いので、最後の 4 小節を繰り返して重ねる")
        shift_out = best


def _cut_pick(cands: list[dict], center: int, lo: int, hi: int, ok) -> dict | None:
    """center の前後 [lo, hi] 小節の候補から、点数が高く center に近いもの。"""
    best = None
    for c in cands:
        d = c["bar"] - center
        if lo <= d <= hi and ok(c):
            v = c["score"] - 0.12 * abs(d)
            if best is None or v > best[0]:
                best = (v, c)
    return best[1] if best else None


def _make_cut(ta: dict, tb: dict, j: dict, forced: bool) -> bool:
    """a→b をカットインにする（design_cutin.md ②）: 前の曲の終わりを「キレイに切れる小節の頭」へ、次の曲の頭を「入りの良い小節の頭」へ寄せる。
    おまかせ（forced=False）は、良い切り替えポイントが見つかったときだけカットインにする（無ければエコーアウトのまま）。"""
    from ..analysis import cutpoints
    ca, cb = cutpoints.for_track(ta["id"]), cutpoints.for_track(tb["id"])
    if not ca or not cb or not ca["outs"] or not cb["ins"]:
        if not forced:
            return False
        ca, cb = ca or {"outs": [], "bars": []}, cb or {"ins": [], "bars": []}
    ua, ub = dict(ta["use"]), dict(tb["use"])
    keep_a = (ta.get("seg") or {}).get("pos") == "custom"       # 自分で選んだ区間は動かさない
    keep_b = (tb.get("seg") or {}).get("pos") == "custom"
    bt_a = [c["t"] for c in ca["bars"]]
    bt_b = [c["t"] for c in cb["bars"]]
    # 使う範囲は 60 秒（短い曲は今の長さの 6 割）以上残す
    keep_ua = min(CUT_MIN_USE, 0.6 * (ua["t1"] - ua["t0"]))
    keep_ub = min(CUT_MIN_USE, 0.6 * (ub["t1"] - ub["t0"]))
    co = None if keep_a else _cut_pick(ca["outs"], ua["bar1"], *CUT_SEARCH_OUT, lambda c: c["t"] - ua["t0"] >= keep_ua)
    ci = None if keep_b else _cut_pick(cb["ins"], ub["bar0"], *CUT_SEARCH_IN, lambda c: ub["t1"] - c["t"] >= keep_ub)
    if not forced and (co is None or co["score"] < cutpoints.GOOD or ci is None or ci["score"] < 2):
        return False
    tags = []
    if co is not None and co["bar"] != ua["bar1"]:
        ua.update(bar1=co["bar"], t1=round(co["t"], 3))
        ua["notes"] = list(ua.get("notes", [])) + ["カットインの切り替えポイントで終える"]
        ta["use"] = ua
    elif bt_a and ua["bar1"] < len(bt_a):
        co = next((c for c in ca["outs"] if c["bar"] == ua["bar1"]), None)
    if co is not None:
        tags += co["tags"]
    if ci is not None and ci["bar"] != ub["bar0"]:
        ub.update(bar0=ci["bar"], t0=round(ci["t"], 3))
        ub["notes"] = list(ub.get("notes", [])) + ["カットインの入りから始める"]
        tb["use"] = ub
    elif bt_b and ub["bar0"] < len(bt_b):
        ci = next((c for c in cb["ins"] if c["bar"] == ub["bar0"]), None)
    j.update(tpl="cut", bars=0, mode="cut")
    j["params"] = {**j["params"], "tempo": "sync", "loop": 0, "shift": 0, "parts": "none", "cutTrim": "auto"}
    j["why"] = [w for w in j["why"] if not w.startswith(("拍を合わせられない", "テンポランプ", "ボーカルが重なる"))]
    j["why"].append("カットイン（前の曲の小節の頭で切り、次の曲を 1 拍目から。音量は自動で合わせる）"
                    + (f"：{'・'.join(dict.fromkeys(tags + (ci['tags'] if ci else [])))}" if tags or ci else ""))
    return True


def _choose_parts(M: Matrix, a: dict, b: dict, j: dict) -> None:
    """パートごとに鳴らす繋ぎ（Phase 5 B2。design_phase5.md §B2）を選ぶ。設定 auto_parts が入のときだけ。
    ロングブレンド → 入れ替え（前の曲の歌を先に抜き、次の曲はドラムから。ベースは入れ替えの位置で交代）、
    ショートブレンドで歌がまだ重なる → 前の曲の歌を先に抜く。分離した音は、再生・書き出しのときに用意する（1 曲 20 秒ほど）。"""
    j["params"].setdefault("parts", "none")
    if not getattr(M, "auto_parts", True) or j["tpl"] not in ("long", "short") or j["bars"] < 8:
        return                                  # 4 小節の重なりでは、パートを順に入れる時間が無い
    if j["tpl"] == "long" and j["bars"] >= 12:
        j["params"]["parts"] = "swap"
        j["why"].append("パートごとに入れ替える（前の曲の歌を先に抜き、次の曲はドラムから）")
    elif a.get("vocals") and b.get("vocals") and best_vocal_fit(a["vocals"], b["vocals"], j["bars"])["clash"] > 0:
        j["params"]["parts"] = "vocal"
        j["why"].append("歌が重なるので、前の曲の歌を先に抜く")


def best_vocal_fit(vo: list[float], vi: list[float], bars: int, max_trim: int = 16, min_left: int = 8) -> dict:
    """ボーカルが重ならない重なりの長さと位置を探す（design_phase4.md §3.2、つなぎ目エディタの「ずらす」と同じ）。
    vo: 前の曲の使う範囲の小節ごとのボーカルの量、vi: 次の曲の同じもの。前の曲の終わりを最大 max_trim 小節早め、重なりを 16/12/8 小節から選ぶ。
    点数 = 歌の重なり 1 小節 10 ＋ 前の曲を 1 小節削る 0.2 ＋ 重なりを 1 小節短くする 0.3。戻り値 {bars, trim, clash, clash0}。"""
    def clash(trim, n):
        end = len(vo) - trim
        return sum(1 for k in range(n) if 0 <= end - n + k < len(vo) and k < len(vi) and vo[end - n + k] > VOCAL_ON and vi[k] > VOCAL_ON)
    c0 = clash(0, bars)
    best = {"bars": bars, "trim": 0, "clash": c0, "clash0": c0, "cost": c0 * 10}
    if c0 == 0:
        return best
    for n in sorted({x for x in (bars, 12, 8) if x <= bars}, reverse=True):
        for trim in range(0, max_trim + 1):
            if len(vo) - trim < n + min_left:
                break
            c = clash(trim, n)
            cost = c * 10 + trim * 0.2 + (bars - n) * 0.3
            if cost < best["cost"]:
                best = {"bars": n, "trim": trim, "clash": c, "clash0": c0, "cost": cost}
    return best


def _avoid_vocal_clash(ta: dict, tb: dict, a: dict, b: dict, j: dict) -> None:
    """並びが決まった後: 歌もの同士のつなぎで、ボーカルの重なりの少ない位置・長さへ寄せる（前の曲の使う範囲の終わりと重なりの小節数を変える）。"""
    if j["tpl"] not in ("long", "short", "bass") or not a.get("vocals") or not b.get("vocals"):
        return
    ub = ta["use"]["bar1"] - ta["use"]["bar0"]
    fit = best_vocal_fit(a["vocals"], b["vocals"], j["bars"], max_trim=min(16, ub // 4), min_left=min(8, max(4, ub // 2)))
    if fit["clash"] >= fit["clash0"] or (fit["trim"] == 0 and fit["bars"] == j["bars"]):
        return
    if fit["trim"]:
        ca = cache.read(ta["id"])
        if ca is None:
            return
        bt = [x["time"] for x in ca["bars"]] + [ca["duration"]]
        u = dict(ta["use"])
        u["bar1"] = u["bar1"] - fit["trim"]
        u["t1"] = round(bt[u["bar1"]], 3)
        u["notes"] = list(u.get("notes", [])) + [f"ボーカルの重なりを避けて {fit['trim']} 小節早く終える"]
        ta["use"] = u
    j["bars"] = fit["bars"]
    j["why"] = [w for w in j["why"] if not w.startswith("ボーカルが重なる")] + [
        f"ボーカルの重なりを {fit['clash0']} → {fit['clash']} 小節に（重なり {fit['bars']} 小節" + (f"・前の曲を {fit['trim']} 小節早く終える" if fit["trim"] else "") + "）"]


def _envelope(tid: int, use: dict) -> list[float]:
    """タイムラインの帯に描く、使う範囲の小節ごとの音量（最大 160 点）。"""
    f = cache.bars_array(tid, len(BAR_COLUMNS))
    if f is None:
        return []
    v = f[use["bar0"]:use["bar1"], 0]
    if len(v) > 160:
        v = v[: len(v) // (len(v) // 160 + 1) * (len(v) // 160 + 1)].reshape(-1, len(v) // 160 + 1).max(axis=1)
    return [round(float(x), 3) for x in v]


def curve_flow(curve: list[float]):
    """自分で描いた流れ（design_phase3.md §7。0..1 のエネルギーを時間に等間隔で並べたもの）→ 流れの関数。起点の曲のエネルギーには合わせない。"""
    c = np.clip(np.asarray(curve, dtype=float), 0, 1)
    grid = np.linspace(0, 1, len(c))
    return lambda x, e0: np.interp(x, grid, c)


RATING_BONUS = {5: 0.05, 4: 0.025, 3: 0.0, 2: -0.025, 1: -0.05}   # ★ の多い曲を優先（design_phase4.md §1.5）
PLAYS_BONUS = 0.03                # よく聴く曲を少し優先（design_phase5.md §E-3。再生回数の対数を 95 パーセンタイルで 0..1 に）


def apply_prefs(pool: Pool, prefs_opt: dict | None, db_path=None) -> tuple[set[int], dict[int, float]]:
    """好み（Phase 4）: 使わない曲（★1・最近かけた曲）と、★ による相性の上乗せ。pool の要約に rating も入れる。"""
    from .. import prefs
    o = prefs_opt or {}
    ratings = prefs.ratings(db_path)
    for t, d in pool.d.items():
        d["rating"] = ratings.get(t, 0)
    skip = set()
    if o.get("skip_rating1", True):
        skip |= {t for t, r in ratings.items() if r == 1}
    days = float(o.get("avoid_recent_days") or 0)
    if days > 0:
        skip |= prefs.recent(days, db_path)
    bonus = {t: RATING_BONUS.get(r, 0.0) for t, r in ratings.items()} if o.get("prefer_rating", True) else {}
    if o.get("prefer_plays", True):
        pc = prefs.play_counts(db_path)
        if pc:
            top = float(np.percentile(np.log1p(list(pc.values())), 95)) or 1.0
            for t, n in pc.items():
                bonus[t] = bonus.get(t, 0.0) + PLAYS_BONUS * min(1.0, math.log1p(n) / top)
    return skip, bonus


def with_segments(pool, ids: list[int], track_len: float | None, track_pos: str = "auto"):
    """1 曲あたりの長さ・使う区間（I-7）を指定されたら、ids の要約の使う範囲と繋ぐ部分を差し替えた見かけの pool を返す。"""
    if not track_len and (track_pos or "auto") == "auto":
        return pool
    from types import SimpleNamespace
    from . import segments
    D = {}
    for t in ids:
        v = segments.variant(t, pool.d[t], track_len, track_pos)
        D[t] = {**pool.d[t], **(v or {"seg": {"len": track_len, "pos": track_pos}})}
    return SimpleNamespace(d=D)


def rejoin(spec: list[dict], db_path=None, prefs_opt: dict | None = None, join_mode: str = "auto") -> dict:
    """並びはそのままで、曲ごとの区間（{id, len, pos}）を変えて繋ぎを計算し直す（結果の「区間」の切り替え。I-7）。"""
    from types import SimpleNamespace
    from . import segments
    pool = get_pool(db_path)
    apply_prefs(pool, {**(prefs_opt or {}), "avoid_recent_days": 0}, db_path)       # ★ を要約に入れる
    spec = [s for s in spec if int(s["id"]) in pool.d]
    if not spec:
        raise ValueError("解析済みの曲がありません")
    D = {}
    for s in spec:
        t = int(s["id"])
        v = segments.variant(t, pool.d[t], s.get("len") or None, s.get("pos") or "auto", s.get("t0"), s.get("t1"))
        D[t] = {**pool.d[t], **(v or {})}
    ids = [int(s["id"]) for s in spec]
    M = Matrix(SimpleNamespace(d=D), ids, join_mode)
    M.auto_parts = (prefs_opt or {}).get("auto_parts", True)
    # つなぎ目ごとのつなぎ方（'cut' / 'mix'。s["jm"] はその曲から次の曲への繋ぎ）
    modes = {n: s["jm"] for n, s in enumerate(spec) if s.get("jm") in ("cut", "mix")}
    return _pack(M, list(range(len(ids))), modes=modes)


def segment_choices(tid: int, length: float | None, db_path=None) -> list[dict]:
    from . import segments
    pool = get_pool(db_path)
    if tid not in pool.d:
        return []
    return segments.choices(tid, pool.d[tid], length)


def _cover_albums(M: Matrix, seq: list[int], album: np.ndarray, allowed: np.ndarray) -> list[int]:
    """並びに入らなかったアルバムの曲を、前後の相性が一番落ちない所へ 1 曲ずつ差し込む（起点の曲の前には入れない）。"""
    seq = list(seq)
    for a in sorted(set(album[allowed].tolist()) - set(album[seq].tolist())):
        best = None
        for t in np.where(allowed & (album == a))[0]:
            for p in range(1, len(seq) + 1):
                prev, nxt = seq[p - 1], (seq[p] if p < len(seq) else None)
                gain = M.S[prev, t] + (M.S[t, nxt] - M.S[prev, nxt] if nxt is not None else 0.0)
                if best is None or gain > best[0]:
                    best = (gain, int(t), p)
        if best:
            seq.insert(best[2], best[1])
    return seq


def _auto_seeds(M: Matrix, cand: list[int], flow: str, curve, album: np.ndarray, n: int = 3) -> list[int]:
    """起点の曲を自動で選ぶ: 流れの始まりに合うエネルギーの曲（上げていく → 静かな曲、落ち着かせる → 盛り上がった曲）。
    3 案が違う始まりになるよう、別のアルバムから順に選ぶ。"""
    e = M.energy[cand]
    if flow == "up":
        want = np.quantile(e, 0.15)
    elif flow == "down":
        want = np.quantile(e, 0.85)
    elif flow == "custom" and curve:
        want = np.quantile(e, min(1.0, max(0.0, float(curve[0]))))
    else:
        want = np.quantile(e, 0.5)
    order = [cand[k] for k in np.argsort(np.abs(e - want))]
    out, seen = [], set()
    for k in order:
        if album[k] not in seen:
            out.append(k)
            seen.add(album[k])
        if len(out) >= n:
            break
    return out + [k for k in order if k not in out][: n - len(out)]


def plan_albums(albums: list[str], minutes: float, flow: str, seed: int | None = None, use_all: bool = False, avoid: list[int] = (),
                db_path=None, curve: list[float] | None = None, prefs_opt: dict | None = None,
                track_len: float | None = None, track_pos: str = "auto", join_mode: str = "auto") -> dict:
    """アルバムで選ぶ（2026-10-25）: 指定したアルバム（3 枚以上）の曲だけで、おまかせの 3 案を作る。
    - テンポで候補を外さない（曲が少ないため。テンポの合わない所はエコーアウト・カットインで繋ぐ）
    - どのアルバムからも最低 1 曲入れ、同じアルバムが続きにくくする
    - 起点の曲は指定が無ければ自動（流れの始まりに合う曲。案ごとに別のアルバムから）
    - use_all: 選んだアルバムの曲をすべて並べる（全体の長さは使わない）"""
    t0 = time.time()
    albums = list(dict.fromkeys(a for a in albums if a))
    if len(albums) < MIN_ALBUMS:
        raise ValueError(f"アルバムを {MIN_ALBUMS} 枚以上選んでください")
    pool = get_pool(db_path)
    # 自分で選んだアルバムなので「最近かけた曲は避ける」は使わない（★1 を使わない・★の優先はそのまま）
    skip, bonus_map = apply_prefs(pool, {**(prefs_opt or {}), "avoid_recent_days": 0}, db_path)
    aset = set(albums)
    ids = [t for t, d in pool.d.items() if d.get("album") in aset and t not in skip]
    have = {pool.d[t]["album"] for t in ids}
    missing = [a for a in albums if a not in have]
    if len(have) < MIN_ALBUMS:
        raise ValueError(f"解析済みの曲があるアルバムが {len(have)} 枚しかありません（{MIN_ALBUMS} 枚以上必要。解析を進めてください）")
    if use_all and len(ids) > MAX_ALL_TRACKS:
        raise ValueError(f"全曲を並べられるのは {MAX_ALL_TRACKS} 曲までです（{len(ids)} 曲）")
    if seed is not None and seed not in ids:
        seed = None
    if seed is not None:
        ids = [seed] + [t for t in ids if t != seed]
    sp = with_segments(pool, ids, track_len, track_pos)
    M = Matrix(sp, ids, join_mode)
    M.auto_parts = (prefs_opt or {}).get("auto_parts", True)
    names = sorted(have)
    album = np.array([names.index(pool.d[t]["album"]) for t in ids])
    allowed = np.ones(len(ids), bool)
    bonus_v = np.array([bonus_map.get(t, 0.0) for t in ids])
    total = float(M.use_len.sum()) - len(ids) * float(np.median(M.bar_sec)) * 8      # 全曲を繋いだときのおおよその長さ
    target = total if use_all else minutes * 60
    seeds = [0] * 3 if seed is not None else _auto_seeds(M, list(range(len(ids))), flow, curve, album)
    avoid_v = np.zeros(len(ids))
    for t in avoid:
        if t in M.pos:
            avoid_v[M.pos[t]] += 0.2
    fl = curve_flow(curve) if flow == "custom" and curve else flow
    stop = (lambda seq, length: len(seq) >= len(ids)) if use_all else (lambda seq, length: length >= target or len(seq) >= len(ids))
    plans = []
    for k in range(3 if not use_all or seed is None else 1):
        st = seeds[k % len(seeds)]
        res = _beam(M, st, stop, fl, target, avoid_v, width=max(BEAM, 2 * len(ids)) if use_all else BEAM, bonus=bonus_v, album=album)
        if not res:
            break
        seq = _cover_albums(M, res[0][1], album, allowed)
        if any(p["tracks"] and [t["id"] for t in p["tracks"]] == [ids[s] for s in seq] for p in plans):
            continue
        pk = _pack(M, seq)
        pk["albums"] = {a: sum(1 for s in seq if names[album[s]] == a) for a in names}
        plans.append(pk)
        for s in seq[1:]:
            avoid_v[s] += 0.25
    return {"plans": plans, "candidates": len(ids), "albums": len(have), "missing_albums": missing,
            "max_minutes": round(max(0.0, total) / 60, 1), "flow": flow, "sec": round(time.time() - t0, 2)}


def plan_library(seed: int, groups: list[str], minutes: float, flow: str, avoid: list[int] = (), db_path=None,
                 curve: list[float] | None = None, prefs_opt: dict | None = None, playlists: list[int] | None = None,
                 track_len: float | None = None, track_pos: str = "auto", join_mode: str = "auto") -> dict:
    """ライブラリから選んで作る: 互いに曲の半分以上が違う 3 案（design_phase3.md §4.3）。flow='custom' なら curve を目標にする。
    playlists を指定したら、選ぶ範囲はジャンルのまとまりではなく、その iTunes のプレイリストの曲（Phase 5 E）。"""
    t0 = time.time()
    pool = get_pool(db_path)
    if seed not in pool.d:
        raise ValueError("起点の曲がまだ解析されていません")
    sd = pool.d[seed]
    gs = set(groups)
    skip, bonus_map = apply_prefs(pool, prefs_opt, db_path)
    auto_parts = (prefs_opt or {}).get("auto_parts", True)
    if playlists:
        from ..library import itunes
        members = itunes.playlist_tracks(playlists, db_path)
        cands = [t for t in pool.d if t != seed and t not in skip and t in members]
    else:
        cands = [t for t, d in pool.d.items() if t != seed and t not in skip and (not gs or d["group"] in gs)]
    # テンポが起点の曲から倍/半を考慮して ±25% 以内
    # カットインだけで繋ぐなら、テンポの違う曲も選ぶ（±50% 以内。design_cutin.md）
    lim = 0.41 if join_mode == "cut" else 0.23

    def tempo_ok(d):
        return min(abs(math.log(k * sd["bpm"] / d["bpm"])) for k in (1, 2, 0.5)) < lim
    cands = [t for t in cands if tempo_ok(pool.d[t])]
    if len(cands) > MAX_CANDS - 1:     # 多ければ、起点の曲に雰囲気の近い順
        tv = sd["timbre"]
        cands.sort(key=lambda t: float(np.linalg.norm(pool.d[t]["timbre"] - tv)))
        cands = cands[: MAX_CANDS - 1]
    ids = [seed] + cands
    if len(ids) < 3:
        raise ValueError("候補の曲が少なすぎます（範囲を広げるか、ライブラリの解析を進めてください）")
    sp = with_segments(pool, ids, track_len, track_pos)
    from .segments import is_short
    if is_short(track_len) and track_pos != "full":
        # 短い範囲を作れなかった曲（解析の小節が少ないなど）は、元の長い範囲のまま混ざらないよう候補から外す（起点の曲は残す）
        ids = [ids[0]] + [t for t in ids[1:] if sp.d[t]["use"]["t1"] - sp.d[t]["use"]["t0"] <= 2.5 * track_len]
    M = Matrix(sp, ids, join_mode)
    M.auto_parts = auto_parts
    bonus_v = np.array([bonus_map.get(t, 0.0) for t in ids])
    target = minutes * 60
    avoid_v = np.zeros(len(ids))
    for t in avoid:
        if t in M.pos and t != seed:
            avoid_v[M.pos[t]] += 0.2
    plans = []
    for _ in range(3):
        res = _beam(M, 0, lambda seq, length: length >= target, curve_flow(curve) if flow == "custom" and curve else flow, target, avoid_v,
                    bonus=bonus_v)
        if not res:
            break
        seq = res[0][1]
        plans.append(_pack(M, seq))
        for k in seq[1:]:                  # 次の案では、この案の曲を使いにくくする（互いに違う 3 案）
            avoid_v[k] += 0.25
    return {"plans": plans, "candidates": len(ids), "skipped": len(skip - {seed}), "flow": flow, "sec": round(time.time() - t0, 2)}


def plan_queue(ids: list[int], db_path=None, join_mode: str = "auto") -> dict:
    """キューを並べ替える: キューの曲だけで、最初の曲も含めて探す。今の並びも同じ物差しで返す。"""
    t0 = time.time()
    pool = get_pool(db_path)
    have = [t for t in ids if t in pool.d]
    missing = [t for t in ids if t not in pool.d]
    if len(have) < 2:
        raise ValueError("解析済みの曲が 2 曲以上必要です")
    M = Matrix(pool, have, join_mode)
    from .. import settings as _settings
    M.auto_parts = _settings.load(db_path).get("auto_parts", True)
    n = len(have)
    best = None
    for s in range(n):
        res = _beam(M, s, lambda seq, length: len(seq) == n, "keep", 1e9, np.zeros(n), width=max(BEAM, 3 * n), flow_w=0.0)
        if res:
            seq = res[0][1]
            sc = float(np.mean([M.S[a, b] for a, b in zip(seq, seq[1:])]))
            if best is None or sc > best[0]:
                best = (sc, seq)
    current = _pack(M, list(range(n)))
    return {"plans": [_pack(M, best[1]), current], "missing": missing, "sec": round(time.time() - t0, 2)}


from .genres import NAMES as GENRE_NAMES  # noqa: E402


def _norm_artist(a: str | None) -> str:
    return (a or "").strip().casefold()


def candidate_facets(db_path=None) -> dict:
    """次の曲の候補を絞り込む選択肢（解析済みの曲のアーティストとジャンルのまとまり、曲数つき）。"""
    pool = get_pool(db_path)
    arts: dict[str, list] = {}
    groups: dict[str, int] = {}
    for d in pool.d.values():
        a = (d.get("artist") or "").strip()
        if a:
            k = _norm_artist(a)
            if k not in arts:
                arts[k] = [a, 0]
            arts[k][1] += 1
        groups[d["group"]] = groups.get(d["group"], 0) + 1
    return {"artists": [{"name": v[0], "count": v[1]} for v in sorted(arts.values(), key=lambda v: v[0].casefold())],
            "groups": [{"name": g, "count": groups[g]} for g in [*GENRE_NAMES, *sorted(set(groups) - set(GENRE_NAMES))] if g in groups]}


def next_candidates(prev: int | None, nxt: int | None, groups: list[str], exclude: list[int], n: int = 8, db_path=None,
                    prefs_opt: dict | None = None, artists: list[str] | None = None) -> list[dict]:
    """次の曲の候補（案Y）。prev の次に合う曲。nxt もあれば（差し替え）前後の相性の平均で並べる。
    artists を指定したら、そのアーティストの曲だけ（大文字小文字・前後の空白は区別しない）。"""
    pool = get_pool(db_path)
    gs = set(groups)
    arts = {_norm_artist(a) for a in artists or [] if _norm_artist(a)}
    ex = set(exclude) | {prev, nxt}
    base = [t for t in (prev, nxt) if t in pool.d]
    if not base:
        return []
    ref = pool.d[base[0]]
    skip, bonus_map = apply_prefs(pool, {**(prefs_opt or {}), "avoid_recent_days": 0}, db_path)   # 自分で探すときは最近の曲も出す
    cands = [t for t, d in pool.d.items() if t not in ex and t not in skip and (not gs or d["group"] in gs)
             and (not arts or _norm_artist(d.get("artist")) in arts)
             and min(abs(math.log(k * ref["bpm"] / d["bpm"])) for k in (1, 2, 0.5)) < 0.3]
    if len(cands) > 1500:
        tv = ref["timbre"]
        cands.sort(key=lambda t: float(np.linalg.norm(pool.d[t]["timbre"] - tv)))
        cands = cands[:1500]
    M = Matrix(pool, base + cands)
    off = len(base)
    sc = np.zeros(len(cands))
    if prev in pool.d:
        sc += M.S[M.pos[prev], off:]
    if nxt in pool.d:
        sc += M.S[off:, M.pos[nxt]]
    sc /= len(base)
    sc += np.array([bonus_map.get(t, 0.0) for t in cands])
    out = []
    for k in np.argsort(-sc)[:n]:
        t = cands[k]
        d = pool.d[t]
        row = {"id": t, "title": d.get("title"), "artist": d.get("artist"), "album": d.get("album"), "genre": d.get("genre"), "bpm": d["bpm"], "rating": d.get("rating", 0),
               "key": d["key"], "energy": round(d["energy"], 3), "use": d["use"], "duration": d["duration"],
               "env": _envelope(t, d["use"]), "score": round(float(sc[k]), 3)}
        if prev in pool.d:
            row["join"] = M.join(prev, t)
            row["energy_delta"] = round(d["energy"] - pool.d[prev]["energy"], 3)
        if nxt in pool.d:
            row["join_next"] = M.join(t, nxt)
        out.append(row)
    return out
