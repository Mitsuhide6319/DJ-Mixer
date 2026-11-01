"""起動: uvicorn をバックグラウンドスレッドで立ち上げ、pywebview の専用ウィンドウで画面を開く。

    uv run python -m djmix              # 専用ウィンドウ（通常）
    uv run python -m djmix --browser    # 既定のブラウザで開く（開発用）
    uv run python -m djmix --server     # サーバーだけ起動（テスト用）
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .config import APP_TITLE, DATA_DIR, HOST

log = logging.getLogger("djmix")


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


STARTUP_TIMEOUT = 180.0     # その日最初の起動はライブラリの読み込み（ディスク・ウイルス対策の検査）で 20 秒ほどかかることがある


def wait_ready(url: str, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"サーバーが {timeout} 秒以内に起動しませんでした: {url}")


ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
APP_ID = "DJMixer.App"


def _set_app_id() -> None:
    """タスクバーで Python とは別のアプリとして扱わせる（Python のアイコンでなく、このアプリのアイコンでまとまる）。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _set_window_icon(title: str) -> None:
    """専用ウィンドウ（タイトル title）にアプリのアイコンを付ける（ウィンドウの左上・タスクバー・Alt＋Tab）。ウィンドウができるまで待つ。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.LoadImageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        if not ICON.exists():
            return
        from .winshell import find_own_window, set_window_app_id
        hwnd = None
        for _ in range(100):                    # 最大 10 秒（このプロセスの、タイトルが title の見えているウィンドウ）
            hwnd = find_own_window(title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            log.warning("window not found for icon")
            return
        # Store 版 Python で動いているので、ウィンドウそのものに AppUserModelID を付ける（タスクバーで Python のアイコンにならないように）。
        # ピン留めしたときは DJ Mixer.vbs で起動し、このアイコン・名前で出る
        root = Path(__file__).resolve().parent.parent
        ok = set_window_app_id(hwnd, APP_ID, relaunch=f'wscript.exe "{root / "DJ Mixer.vbs"}"', icon=f"{ICON},0", name=APP_TITLE)
        log.info("window app id set: %s", ok)
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        for kind, size in ((1, 32), (0, 16)):   # ICON_BIG（タスクバー・Alt＋Tab）、ICON_SMALL（ウィンドウの左上）
            h = u.LoadImageW(None, str(ICON), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                u.SendMessageW(hwnd, WM_SETICON, kind, h)
    except Exception:
        log.exception("window icon")


def _already_running() -> bool:
    """もう起動しているなら、そのウィンドウを前に出して True（二重に起動しない）。"""
    try:
        import ctypes
        k, u = ctypes.windll.kernel32, ctypes.windll.user32
        k.CreateMutexW.restype = ctypes.c_void_p
        _already_running.mutex = k.CreateMutexW(None, False, "Local\\DJMixer.SingleInstance")   # プロセスが終わるまで持つ
        if k.GetLastError() != 183:          # ERROR_ALREADY_EXISTS
            return False
        u.FindWindowW.restype = ctypes.c_void_p
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        for _ in range(50):                  # 先に起動した方の窓がまだできていなければ少し待つ
            h = u.FindWindowW(None, APP_TITLE)
            if h:
                u.ShowWindow(ctypes.c_void_p(h), 9)          # SW_RESTORE（最小化していたら戻す）
                u.SetForegroundWindow(ctypes.c_void_p(h))
                break
            time.sleep(0.1)
        return True
    except Exception:
        return False


def _splash() -> str:
    """サーバーが立ち上がるまで見せる画面（すぐ出す。経過秒つき）。"""
    import base64
    icon = ICON.parent / "icons" / "a" / "96.png"
    img = f'<img src="data:image/png;base64,{base64.b64encode(icon.read_bytes()).decode()}" width="96" height="96">' if icon.exists() else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;height:100%;background:#0b0c0e;color:#e8e8e8;font-family:"Yu Gothic UI","Segoe UI",sans-serif}}
.c{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}}
.t{{font-size:20px;letter-spacing:.06em}}.s{{color:#8a9099;font-size:13px}}
.bar{{width:260px;height:4px;border-radius:2px;background:#22262c;overflow:hidden}}
.bar i{{display:block;width:40%;height:100%;background:linear-gradient(90deg,#22d3ee,#fb923c);animation:m 1.2s ease-in-out infinite alternate}}
@keyframes m{{from{{margin-left:0}}to{{margin-left:60%}}}}
</style></head><body><div class="c">{img}<div class="t" id="t">DJ Mixer を起動しています…</div>
<div class="bar"><i></i></div><div class="s" id="s">0 秒</div>
<div class="s">その日はじめての起動は 20 秒ほどかかることがあります</div></div>
<script>const t0=Date.now();setInterval(()=>{{document.getElementById('s').textContent=Math.floor((Date.now()-t0)/1000)+' 秒'}},500);</script>
</body></html>"""


def _alert(msg: str) -> None:
    """コンソールなしで起動したときも分かるよう、起動の失敗はメッセージの窓で知らせる（Windows）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_TITLE, 0x10)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="djmix")
    ap.add_argument("--port", type=int, default=0, help="0 なら空きポートを自動選択")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true", help="専用ウィンドウでなく既定のブラウザで開く")
    mode.add_argument("--server", action="store_true", help="サーバーだけ起動する")
    ap.add_argument("--debug", action="store_true", help="WebView の開発者ツールを有効にする")
    args = ap.parse_args()

    from .procutil import ensure_std_streams
    no_console = sys.stderr is None
    ensure_std_streams()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # コンソールなし（pythonw。「DJ Mixer.vbs」から起動）のときは画面に書かず、ログのファイルだけ
    handlers: list[logging.Handler] = [logging.FileHandler(DATA_DIR / "djmix.log", encoding="utf-8")]
    if not no_console:
        handlers.insert(0, logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    port = args.port or free_port()
    url = f"http://{HOST}:{port}"
    server = uvicorn.Server(uvicorn.Config("djmix.server:app", host=HOST, port=port, log_level="warning"))

    if args.server:
        log.info("server only: %s", url)
        server.run()
        return

    if not args.browser and _already_running():
        log.info("already running; brought the window to the front")
        return

    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    def fail(e: Exception) -> None:
        log.error("startup failed: %s", e)
        _alert(f"DJ Mixer を起動できませんでした。\n\n{e}\n\n詳しくは {DATA_DIR / 'djmix.log'} を見てください。")

    try:
        if args.browser:
            try:
                wait_ready(url, STARTUP_TIMEOUT)
            except Exception as e:
                fail(e)
                raise
            log.info("server ready: %s", url)
            webbrowser.open(url)
            th.join()
        else:
            import os

            import webview

            # 自分のアプリの画面なので、クリック前でも音声を開始できるよう自動再生制限を外す（WebView2 の起動オプション）
            os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--autoplay-policy=no-user-gesture-required")

            _set_app_id()
            # 窓はすぐ出して「起動しています…」を見せ、サーバーが立ち上がったらアプリの画面に切り替える
            win = webview.create_window(APP_TITLE, html=_splash(), width=1920, height=1080, min_size=(1280, 800),
                                        background_color="#0b0c0e")

            def boot() -> None:
                _set_window_icon(APP_TITLE)    # アイコン（ウィンドウの左上・タスクバー・Alt＋Tab）と、タスクバーでの ID
                try:
                    wait_ready(url, STARTUP_TIMEOUT)
                except Exception as e:
                    fail(e)
                    win.destroy()
                    return
                log.info("server ready: %s", url)
                win.load_url(url)

            webview.start(boot, debug=args.debug, icon=str(ICON) if ICON.exists() else None)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        th.join(timeout=5)


if __name__ == "__main__":
    main()
