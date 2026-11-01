// 書き出し「再生しながら録音する」（design_phase3.md §6.1）。
// マスター出力（リミッターの後）を AudioWorklet で 16bit ステレオの PCM にし、WebSocket でサーバーへ送る（サーバーが WAV に書く）。
// MediaRecorder（Opus に圧縮される）は使わない。

const TAP = `
class Tap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(44100);          // 0.5 秒分（ステレオ）ずつ送る
    this.n = 0;
    this.port.onmessage = e => { if (e.data === 'flush') { this.flush(); this.port.postMessage('flushed'); } };
  }
  flush() {
    if (!this.n) return;
    const out = this.buf.slice(0, this.n);
    this.port.postMessage(out.buffer, [out.buffer]);
    this.n = 0;
  }
  process(inputs) {
    const inp = inputs[0];
    const L = inp && inp[0], R = inp && (inp[1] || inp[0]);
    for (let i = 0; i < 128; i++) {            // 入力が無い（無音）ときも 0 を書いて時間をそろえる
      const l = L ? Math.max(-1, Math.min(1, L[i])) : 0, r = R ? Math.max(-1, Math.min(1, R[i])) : 0;
      this.buf[this.n++] = l < 0 ? l * 32768 : l * 32767;
      this.buf[this.n++] = r < 0 ? r * 32768 : r * 32767;
      if (this.n >= this.buf.length) this.flush();
    }
    return true;
  }
}
registerProcessor('djmix-tap', Tap);`;

let moduleReady = null;

export class MixRecorder {
  constructor(engine) {
    this.engine = engine;
    this.active = false;
    this.tracklist = [];
  }

  get elapsed() { return this.active ? this.engine.ctx.currentTime - this.t0 : 0; }

  /** 録音を始める。o: {folder, name, format}。戻り値: 保存先のパス。 */
  async start(o) {
    const ctx = this.engine.ctx;
    moduleReady ??= ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([TAP], { type: 'text/javascript' })));
    await moduleReady;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/record`);
    ws.binaryType = 'arraybuffer';
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('録音の接続に失敗しました')); });
    const ready = new Promise((res, rej) => { ws.onmessage = e => { const m = JSON.parse(e.data); m.error ? rej(new Error(m.error)) : res(m); }; });
    ws.send(JSON.stringify({ ...o, sampleRate: ctx.sampleRate }));
    const head = await ready;
    const node = new AudioWorkletNode(ctx, 'djmix-tap', { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 2, channelCountMode: 'explicit' });
    node.port.onmessage = e => { if (e.data instanceof ArrayBuffer && ws.readyState === 1) ws.send(e.data); };
    const mute = ctx.createGain();
    mute.gain.value = 0;
    this.engine.masterMeter.connect(node);
    node.connect(mute).connect(ctx.destination);      // 出力をつながないと process() が呼ばれない
    Object.assign(this, { ws, node, mute, active: true, t0: ctx.currentTime, tracklist: [], path: head.path, lastMarked: null });
    return head.path;
  }

  /** 曲が鳴り始めた時刻を曲目リストに記す（同じ曲が続けて呼ばれたら 1 回だけ）。 */
  mark(track) {
    if (!this.active || !track || this.lastMarked === track.id) return;
    this.lastMarked = track.id;
    this.tracklist.push({ t: Math.max(0, this.elapsed), title: track.title, artist: track.artist, album: track.album });
  }

  /** 録音を終えてファイルにする。戻り値: {path, tracklist, seconds}。 */
  async stop(withTracklist = true) {
    if (!this.active) return null;
    this.active = false;
    const { ws, node, mute } = this;
    const flushed = new Promise(res => {
      const prev = node.port.onmessage;
      node.port.onmessage = e => { if (e.data === 'flushed') res(); else prev(e); };
    });
    node.port.postMessage('flush');
    await Promise.race([flushed, new Promise(r => setTimeout(r, 1000))]);
    this.engine.masterMeter.disconnect(node);
    node.disconnect(); mute.disconnect();
    const done = new Promise((res, rej) => { ws.onmessage = e => { const m = JSON.parse(e.data); m.error ? rej(new Error(m.error)) : res(m); }; });
    ws.send(JSON.stringify({ end: true, tracklist: withTracklist ? this.tracklist : null }));
    try { return await done; } finally { ws.close(); }
  }
}
