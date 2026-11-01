"""小節頭の推定を耳で確かめる WAV を書き出す（読むだけ。音は鳴らさない）。

原曲に、小節頭へ高いクリック（1000Hz）、それ以外の拍へ低いクリック（500Hz）を重ねる。
新（拍子の途中変化を許す Viterbi）と今（曲全体で 1 つの拍子）の 2 つを、左右に分けずにそれぞれ別のファイルに書く。

使い方:
  .venv\\Scripts\\python tools\\meter_clicks.py <曲名の一部 または track_id> [--from 秒] [--to 秒] [--out フォルダ]
既定の出力先は work\\meter_clicks（.gitignore 済み）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from djmix.analysis import bars, cache  # noqa: E402
from djmix.audio.decode import SR, decode  # noqa: E402
from djmix.config import DATA_DIR  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def click(freq: float, sec: float = 0.04) -> np.ndarray:
    t = np.arange(int(sec * SR)) / SR
    return (np.sin(2 * np.pi * freq * t) * np.exp(-t * 60)).astype(np.float32)


def overlay(audio: np.ndarray, beats: np.ndarray, heads: set[int], t0: float, t1: float) -> np.ndarray:
    hi, lo = click(1000) * 0.5, click(500) * 0.35
    a0, a1 = int(t0 * SR), int(min(t1, len(audio) / SR) * SR)
    out = audio[a0:a1].copy() * 0.6
    for k, t in enumerate(beats):
        if not t0 <= t < t1:
            continue
        c = hi if k in heads else lo
        i = int((t - t0) * SR)
        n = min(len(c), len(out) - i)
        if n > 0:
            out[i:i + n] += c[:n, None]
    return np.clip(out, -1, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("track")
    ap.add_argument("--from", dest="t0", type=float, default=0.0)
    ap.add_argument("--to", dest="t1", type=float, default=1e9)
    ap.add_argument("--out", default=str(ROOT / "work" / "meter_clicks"))
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{DATA_DIR / 'library.sqlite'}?mode=ro", uri=True)    # 読むだけ
    if a.track.isdigit():
        row = con.execute("SELECT id, title, artist, path FROM tracks WHERE id = ?", (int(a.track),)).fetchone()
    else:
        row = con.execute("SELECT id, title, artist, path FROM tracks WHERE title LIKE ? AND analysis_status = 'done' ORDER BY id",
                          (f"%{a.track}%",)).fetchone()
    if not row:
        sys.exit("曲が見つかりません")
    tid, title, artist, path = row
    an, dp = cache.read(tid), cache.downbeat_prob(tid)
    b = np.asarray(an["beats"], dtype=float)
    s = bars.downbeat_strength(b, dp)
    new = bars.bar_phases_varying(b, s)
    audio = decode(path)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in f"{title} - {artist}" if c not in '\\/:*?"<>|')[:60]
    for tag, heads in (("新_Viterbi", {x["beat"] for x in new}), ("今_拍子1つ", {x["beat"] for x in an["bars"]})):
        f = out / f"{tid}_{safe}_{tag}.wav"
        sf.write(f, overlay(audio, b, heads, a.t0, a.t1), SR)
        print(f)
    ms = [x["meter"] for x in new]
    print(f"{title} / {artist}: 新の小節ごとの拍子", " ".join(f"{x['time']:.0f}s:{x['meter']}" for k, x in enumerate(new) if k == 0 or ms[k] != ms[k - 1]))


if __name__ == "__main__":
    main()
