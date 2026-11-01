"""アイコンを作る: assets/icons/icon_*.svg を、Edge（ヘッドレス）で大きさごとに描いて PNG に、まとめて .ico にする。

    uv run python tools/make_icons.py            # すべての案（a, b, c）
    uv run python tools/make_icons.py a          # 案 a だけ

小さい大きさ（16〜48px）も縮小ではなくその大きさで描く（線がにじまないように）。
出力: assets/icons/<案>/<大きさ>.png、assets/icons/<案>.ico（16〜256px）
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]


def edge() -> Path:
    for p in EDGE:
        if p.exists():
            return p
    raise SystemExit("Microsoft Edge が見つかりません")


def render(svg: Path, size: int, out: Path, profile: Path) -> None:
    out.unlink(missing_ok=True)
    page = out.with_suffix(".html")
    page.write_text(f'<!doctype html><html><body style="margin:0;background:transparent;overflow:hidden">'
                    f'<img src="{svg.as_uri()}" width="{size}" height="{size}" style="display:block"></body></html>', encoding="utf-8")
    subprocess.run([str(edge()), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--default-background-color=00000000", f"--user-data-dir={profile}", f"--window-size={size},{size}",
                    f"--screenshot={out}", page.as_uri()], check=True, capture_output=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # msedge.exe はすぐ戻り、描画は別のプロセスが続ける。ファイルができて大きさが落ち着くまで待つ
    import time
    last = -1
    for _ in range(300):
        if out.exists() and out.stat().st_size > 0 and out.stat().st_size == last:
            break
        last = out.stat().st_size if out.exists() else -1
        time.sleep(0.1)
    else:
        raise RuntimeError(f"描けませんでした: {out}")
    page.unlink(missing_ok=True)
    im = Image.open(out).convert("RGBA")
    if im.size != (size, size):
        im = im.crop((0, 0, size, size))
    im.save(out)


def build(key: str) -> Path:
    svg = ICONS / f"icon_{key}.svg"
    outdir = ICONS / key
    outdir.mkdir(exist_ok=True)
    profile = Path(tempfile.gettempdir()) / "djmix_icon_edge_profile"      # Edge が終わった後もしばらく掴むので消さずに使い回す
    for s in SIZES:
        render(svg, s, outdir / f"{s}.png", profile)
    imgs = [Image.open(outdir / f"{s}.png").convert("RGBA") for s in SIZES]
    ico = ICONS / f"{key}.ico"
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
    return ico


if __name__ == "__main__":
    keys = sys.argv[1:] or sorted(p.stem.split("_")[1] for p in ICONS.glob("icon_*.svg"))
    for k in keys:
        print(build(k))
