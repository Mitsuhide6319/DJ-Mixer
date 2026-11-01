"""書き出しの保存（design_phase3.md §6）。WAV（44.1kHz 16bit ステレオ）に少しずつ書き、MP3 なら最後に ffmpeg で変換する。
曲目リスト（開始時刻つき）も同じ名前の .txt に書く。"""
from __future__ import annotations

import os
import re
import time
import wave
from pathlib import Path

from .. import procutil

FORMATS = {"wav": None, "mp3_320": "320k", "mp3_192": "192k"}


def default_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Music" / "DJ Mixer"


def target_path(folder: str | None, name: str | None, fmt: str) -> Path:
    """保存先。フォルダが無ければ作る。同じ名前があれば (2)・(3)… を付ける。"""
    d = Path(folder).expanduser() if folder else default_dir()
    d.mkdir(parents=True, exist_ok=True)
    # ファイル名に使えない文字（\ と / を含む）を _ に。先頭の . も取る（フォルダの外へ出る名前にしない）
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or time.strftime("%Y-%m-%d %H%M ミックス")).strip()).lstrip(". ") or "ミックス"
    ext = ".wav" if fmt == "wav" else ".mp3"
    p, n = d / f"{base}{ext}", 2
    while p.exists():
        p, n = d / f"{base} ({n}){ext}", n + 1
    return p


class WavSink:
    """16bit ステレオの PCM（バイト列）を受け取って WAV に書く。MP3 の指定なら一時 WAV に書いて最後に変換する。"""

    def __init__(self, out: Path, fmt: str, sample_rate: int = 44100):
        self.out, self.fmt, self.sr = out, fmt, sample_rate
        self.wav_path = out if fmt == "wav" else out.with_suffix(".tmp.wav")
        self.w = wave.open(str(self.wav_path), "wb")
        self.w.setnchannels(2)
        self.w.setsampwidth(2)
        self.w.setframerate(sample_rate)
        self.frames = 0

    def write(self, pcm16: bytes) -> None:
        self.w.writeframes(pcm16)
        self.frames += len(pcm16) // 4

    def close(self, tracklist: list[dict] | None = None) -> dict:
        self.w.close()
        if self.fmt != "wav":
            r = procutil.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(self.wav_path), "-codec:a", "libmp3lame",
                                "-b:a", FORMATS[self.fmt], str(self.out)], capture_output=True)
            if r.returncode != 0:
                raise RuntimeError("MP3 への変換に失敗しました: " + r.stderr.decode("utf-8", "replace")[-300:])
            self.wav_path.unlink(missing_ok=True)
        txt = None
        if tracklist:
            txt = self.out.with_suffix(".txt")
            txt.write_text(tracklist_text(tracklist), encoding="utf-8")
        return {"path": str(self.out), "tracklist": str(txt) if txt else None, "seconds": round(self.frames / self.sr, 1)}


def tracklist_text(items: list[dict]) -> str:
    lines = []
    for it in items:
        t = max(0, int(it.get("t", 0)))
        stamp = f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"
        who = f" - {it['artist']}" if it.get("artist") else ""
        album = f" 『{it['album']}』" if it.get("album") else ""          # アルバム名（使ってみての改善）
        lines.append(f"{stamp}  {it.get('title', '')}{who}{album}")
    return "\n".join(lines) + "\n"
