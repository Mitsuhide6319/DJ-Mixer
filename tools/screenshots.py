"""README 用のスクリーンショットを撮る（2026-10-29）。

画面のないブラウザ（Edge headless、音は --mute-audio で出さない）を DevTools Protocol で動かし、
起動中のアプリ（既定は http://127.0.0.1:8768。試験用のデータの複製で起動しておく）の各画面を PNG に保存する。

使い方:
  .venv\\Scripts\\python tools\\screenshots.py [--url http://127.0.0.1:8768] [--out docs\\screenshots] [--only 名前 ...]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
PORT = 9333
W, H = 1920, 1080


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv(timeout=180))
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def js(self, expr: str):
        """式を評価する（Promise は待つ）。例外は Python の例外にする。"""
        r = self.call("Runtime.evaluate", expression=f"(async () => {{ {expr} }})()", awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"])
        return r.get("result", {}).get("value")

    def shot(self, path: Path) -> None:
        self.js(MASK)                                # 写るパスの Windows のユーザー名を伏せる
        path.parent.mkdir(parents=True, exist_ok=True)
        r = self.call("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))
        print("saved", path.relative_to(ROOT))


# 撮る前に、画面の文字と入力欄のパスに含まれる Windows のユーザー名を伏せる（共有するときに困らないように。2026-11-01）
MASK = r"""
  const re = /([A-Za-z]:[\\/]Users[\\/])[^\\/\s"'）)]+/g, to = '$1（ユーザー名）';
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) if (re.test(n.nodeValue)) { re.lastIndex = 0; n.nodeValue = n.nodeValue.replace(re, to); }
  for (const i of document.querySelectorAll('input')) { re.lastIndex = 0; if (re.test(i.value)) { re.lastIndex = 0; i.value = i.value.replace(re, to); } re.lastIndex = 0; if (re.test(i.placeholder)) { re.lastIndex = 0; i.placeholder = i.placeholder.replace(re, to); } }
  for (const s of document.querySelectorAll('select option')) { re.lastIndex = 0; if (re.test(s.textContent)) { re.lastIndex = 0; s.textContent = s.textContent.replace(re, to); } }
  return true;
"""

# 画面ごとの準備（ページの中で動かす JavaScript）。先頭の共通部分で音の出力先を無しにする（--mute-audio に加えて念のため）
PRELUDE = """
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (f, ms = 20000) => { const t = Date.now(); while (!f()) { if (Date.now() - t > ms) throw new Error('timeout'); await sleep(100); } };
  const tab = async name => { [...document.querySelectorAll('.tab')].find(x => x.textContent.trim() === name).click(); await sleep(600); };
  const m = window.__app ??= await import('/app.js');
  await until(() => m.engine);
  try { await m.engine.ctx.setSinkId({ type: 'none' }); } catch {}
  m.engine.master.gain.value = 0;
"""

SCENES: dict[str, str] = {
    # デッキ: キューの 1・2 曲目を A・B に読み込み、曲の途中を表示
    "deck": """
      await tab('ライブラリ');
      const q = (await import('/queue.js')).getItems();
      const a = q[0].track, b = q[1].track;
      const lib = document.getElementById('libsearch');
      for (const [id, t] of [['A', a], ['B', b]]) {
        if (m.engine.decks[id].track?.id === t.id) continue;
        lib.value = t.title; lib.dispatchEvent(new Event('input')); await sleep(1200);
        const row = [...document.querySelectorAll('tr[data-id]')].find(r => +r.dataset.id === t.id);
        row.querySelector(`[data-load=${id}]`).click();
        await until(() => m.engine.decks[id].track?.id === t.id && m.engine.decks[id].analysis, 60000);
      }
      await sleep(3000);
      m.engine.decks.A.seek(64); m.engine.decks.B.seek(40);
      lib.value = ''; lib.dispatchEvent(new Event('input'));
      await tab('デッキ'); await sleep(1500);
    """,
    "library": """
      await tab('ライブラリ');
      const lib = document.getElementById('libsearch'); lib.value = ''; lib.dispatchEvent(new Event('input'));
      document.querySelector('.lfacet [data-k=all], [data-all]')?.click();
      await sleep(2000);
    """,
    "joins-range": "await tab('つなぎ目'); await sleep(2500);",
    "join-editor": """
      await tab('つなぎ目');
      [...document.querySelectorAll('.btn')].find(x => x.offsetParent && x.textContent.trim() === '細かく直す')?.click();
      await sleep(800);
      m.ui.editor.show(2);                      // ロングブレンド 16 小節のつなぎ目
      await sleep(3000);
    """,
    "cutin-editor": """
      const queue = await import('/queue.js');
      queue.patch(1, { join_tpl: 'cut', join_bars: 0 });   // 試験用のデータの複製の中だけ
      await tab('つなぎ目');
      [...document.querySelectorAll('.btn')].find(x => x.offsetParent && x.textContent.trim() === '細かく直す')?.click();
      await sleep(800);
      m.ui.editor.show(1);
      await sleep(3000);
      document.querySelector('[data-r=cutbox]').scrollIntoView({ block: 'end' }); await sleep(800);
    """,
    "auto-result": """
      await tab('おまかせ');
      const root = document.getElementById('autobody');
      root.querySelector('[data-m=lib]').click(); root.querySelector('[data-rg=groups]').click(); await sleep(300);
      root.querySelector('[data-tl="0"]').click(); root.querySelector('[data-jmv=auto]').click();
      root.querySelector('[data-r=go]').click();
      await until(() => root.querySelector('.ptab'), 60000); await sleep(2000);
    """,
    "auto-albums": """
      await tab('おまかせ');
      const root = document.getElementById('autobody');
      root.querySelector('[data-m=lib]').click(); root.querySelector('[data-rg=albums]').click(); await sleep(300);
      root.querySelector('[data-albpick]').click();
      await until(() => document.querySelector('.alblist label'));
      const q = document.querySelector('[data-aq]'); q.value = 'T-SQUARE'; q.dispatchEvent(new Event('input')); await sleep(600);
      for (const n of ['B.C.A.D.', 'BLUE IN RED', 'Gravity', 'Human']) {
        const l = [...document.querySelectorAll('.alblist label')].find(l => l.querySelector('.an').textContent === n); if (l && !l.querySelector('input').checked) l.querySelector('input').click();
      }
      await sleep(600);
    """,
    "mixes": "document.querySelector('[data-close]')?.click(); await tab('ミックス'); await sleep(1200); document.querySelector('.mixlist .mixrow')?.click(); await sleep(1200);",
    "export": "await tab('デッキ'); document.getElementById('qexport').click(); await sleep(1500);",
    "settings": "document.querySelector('[data-close]')?.click(); await sleep(300); await tab('設定'); await sleep(1200);",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8768")
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--only", nargs="*", default=[])
    a = ap.parse_args()
    edge = next((p for p in EDGE if p.exists()), None)
    if edge is None:
        sys.exit("Edge が見つかりません")
    prof = tempfile.mkdtemp(prefix="djmix_shot_")
    proc = subprocess.Popen([str(edge), "--headless=new", "--mute-audio", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                             f"--window-size={W},{H}", "--force-device-scale-factor=1", "--hide-scrollbars",
                             "--autoplay-policy=no-user-gesture-required", "about:blank"])
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        with connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            p = Page(ws)
            p.call("Page.enable")
            p.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
            p.call("Page.navigate", url=a.url)
            time.sleep(4)
            for name, body in SCENES.items():
                if a.only and name not in a.only:
                    continue
                try:
                    p.js(PRELUDE + body)
                    p.shot(Path(a.out) / f"{name}.png")
                except Exception as e:
                    print("failed", name, e)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        import shutil
        time.sleep(1)
        shutil.rmtree(prof, ignore_errors=True)       # 一時のブラウザのプロファイル


if __name__ == "__main__":
    main()
