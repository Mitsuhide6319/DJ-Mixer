"""MIDI の入出力（design_phase1.md §6.2）。python-rtmidi で全入力ポートを監視し、WebSocket で画面に送る。

- 入力ポートの抜き差しは 2 秒ごとに確認して自動で開き直す
- 画面からの MIDI 出力（コントローラーの LED 点灯）も受け付ける
- 実機が無くても試せるよう、inject() で擬似的なメッセージを同じ経路に流せる
WebView2 の Web MIDI の権限挙動に依存しないよう、MIDI はバックエンドで扱う（打合せ6）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

log = logging.getLogger("djmix.midi")


class MidiBridge:
    def __init__(self):
        self.inputs: dict[str, object] = {}     # ポート名 -> rtmidi.MidiIn
        self.outputs: dict[str, object] = {}    # ポート名 -> rtmidi.MidiOut
        self.clients: set = set()               # WebSocket
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.available = True
        self.error: str | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        if self.thread is None:
            self.thread = threading.Thread(target=self._watch, name="midi", daemon=True)
            self.thread.start()

    # ---- ポートの監視 ----
    def _watch(self) -> None:
        try:
            import rtmidi
        except Exception as e:     # rtmidi が使えない環境でもアプリは動かす
            self.available, self.error = False, str(e)
            log.warning("MIDI unavailable: %s", e)
            return
        while True:
            try:
                self._refresh(rtmidi)
            except Exception as e:
                log.exception("MIDI refresh failed")
                self.error = str(e)
            time.sleep(2.0)

    def _refresh(self, rtmidi) -> None:
        probe = rtmidi.MidiIn()
        names = probe.get_ports()
        probe.close_port()
        del probe
        with self.lock:
            for gone in [n for n in self.inputs if n not in names]:
                self.inputs.pop(gone).close_port()
                log.info("MIDI in closed: %s", gone)
            for i, name in enumerate(names):
                if name in self.inputs:
                    continue
                mi = rtmidi.MidiIn()
                mi.open_port(i)
                mi.ignore_types(sysex=True, timing=True, active_sense=True)
                mi.set_callback(self._on_message, name)
                self.inputs[name] = mi
                log.info("MIDI in opened: %s", name)
        oprobe = rtmidi.MidiOut()
        onames = oprobe.get_ports()
        del oprobe
        with self.lock:
            for gone in [n for n in self.outputs if n not in onames]:
                self.outputs.pop(gone).close_port()
            for i, name in enumerate(onames):
                if name not in self.outputs:
                    mo = rtmidi.MidiOut()
                    mo.open_port(i)
                    self.outputs[name] = mo

    def ports(self) -> dict:
        with self.lock:
            return {"available": self.available, "error": self.error,
                    "inputs": sorted(self.inputs), "outputs": sorted(self.outputs)}

    # ---- 入力 → 画面 ----
    def _on_message(self, event, port_name) -> None:
        msg, _delta = event
        self.inject(port_name, list(msg))

    def inject(self, port: str, data: list[int]) -> None:
        """MIDI メッセージを画面へ送る（実機の入力と、試験用の擬似入力の共通経路）。"""
        if self.loop is None:
            return
        payload = {"type": "midi", "port": port, "data": [int(x) & 0xFF for x in data[:3]], "t": time.time()}
        self.loop.call_soon_threadsafe(self._broadcast, payload)

    def _broadcast(self, payload: dict) -> None:
        for ws in list(self.clients):
            asyncio.ensure_future(self._send(ws, payload))

    async def _send(self, ws, payload) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            self.clients.discard(ws)

    # ---- 画面 → 出力（LED） ----
    def send(self, port: str, data: list[int]) -> bool:
        with self.lock:
            mo = self.outputs.get(port)
            if mo is None:
                # 入力と出力でポート名が少し違う機種があるので、前方一致でも探す
                mo = next((o for n, o in self.outputs.items() if n.split(" ")[0] == port.split(" ")[0]), None)
            if mo is None:
                return False
            mo.send_message([int(x) & 0xFF for x in data])
            return True
