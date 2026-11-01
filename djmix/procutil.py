"""外部コマンド（ffmpeg）の起動。コンソールなしで起動したアプリ（pythonw）から呼んでも、黒い窓が出ないようにする。"""
from __future__ import annotations

import subprocess
import sys

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run(args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, creationflags=NO_WINDOW, **kw)


def reveal(path: str) -> str:
    """エクスプローラーで開く。ファイルならそのフォルダを開いてファイルを選んだ状態に、フォルダならそのフォルダを。
    無ければ、ある所まで親をたどって開く。戻り値は開いたもの（'file' / 'folder'）。"""
    import os
    p = os.path.abspath(path)
    # ネットワークのパス（\\server\share、\\?\UNC\…、/\server など）は開かない（開こうとした時点で資格情報が送られるのを避ける）。
    # 文字列の先頭だけで見ると書き方を変えてすり抜けられるので、正規化してからドライブの部分で見る
    drive = os.path.splitdrive(p)[0]
    if drive.startswith(("\\\\", "//")) or p.startswith(("\\\\?\\", "\\\\.\\")):
        raise PermissionError("network path")
    if os.path.isfile(p):
        subprocess.Popen(["explorer", f"/select,{p}"], creationflags=NO_WINDOW)
        return "file"
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            raise FileNotFoundError(path)
        p = parent
    subprocess.Popen(["explorer", p], creationflags=NO_WINDOW)
    return "folder"


def ensure_std_streams() -> None:
    """コンソールなし（pythonw）では sys.stdout / sys.stderr が None になり、uvicorn などが起動時に落ちる。捨て先を付ける。"""
    import os
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
