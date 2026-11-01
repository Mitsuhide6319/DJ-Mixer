// MIDI コントローラー（design_phase1.md §6.2）。MIDI は backend（python-rtmidi）で受け、WebSocket で届く。
//
// 割り当て（mapping）の 1 件: {port, type:'note'|'cc'|'pb', ch, num, action, mode, lsb?}
//   mode: 'button'（押す/離す）| 'abs'（絶対値。cc 7bit / cc 14bit(lsb あり) / pb 14bit）
//         | 'rel-twos' | 'rel-offset' | 'rel-sign'（回し続けるエンコーダー。相対値の 3 方式）
// ・MIDI Learn: learn(action) の後に動かした操作子を割り当てる。種類は届いたメッセージから自動判定する
// ・ソフトテイクオーバー: abs は、ハードの位置が画面の値に追いつくまで反映しない（値の飛びを防ぐ。
//   Phase 3 の「自動再生中に触った部分だけ手動」もこの仕組みの上に作る）
// ・LED: トグル/ボタンの状態が変わったら、割り当て元の操作子へノート/CC を送り返す
import * as actions from './actions.js';

const LEARN_WAIT = 350;       // CC を受けてから種類を判定するまで待つ時間（相対値・14bit の判定用）
const PICKUP = 0.04;          // ソフトテイクオーバーで「追いついた」とみなす差

export function parse(port, data) {
  const [st, d1 = 0, d2 = 0] = data;
  const hi = st & 0xf0, ch = st & 0x0f;
  if (hi === 0x90 || hi === 0x80) return { port, type: 'note', ch, num: d1, value: hi === 0x90 ? d2 : 0 };
  if (hi === 0xb0) return { port, type: 'cc', ch, num: d1, value: d2 };
  if (hi === 0xe0) return { port, type: 'pb', ch, num: 0, value: d1 | (d2 << 7) };
  return null;
}

export function decodeRel(mode, v) {
  if (mode === 'rel-twos') return v < 64 ? v : v - 128;
  if (mode === 'rel-offset') return v - 64;
  if (mode === 'rel-sign') return v & 64 ? -(v & 63) : v;
  return 0;
}

/** 学習中に集めた CC の値から、絶対値か相対値（どの方式か）かを判定する。
 *  回し続けるエンコーダーは同じ値（+1 なら 1 や 65）を繰り返し送る。フェーダー・ノブは毎回違う値で一方向に動く。
 *  中央付近をゆっくり動かしたフェーダー（60, 62…）を「64 中心の相対値」と誤判定しないよう、先にこれで見分ける（P1-9 で確認）。 */
export function classifyCc(values) {
  const repeated = new Set(values).size < values.length;
  const monotonic = values.every((v, i) => i === 0 || v > values[i - 1]) || values.every((v, i) => i === 0 || v < values[i - 1]);
  if (!repeated && (monotonic || values.length < 2)) return 'abs';
  const inSet = (v, ranges) => ranges.some(([a, b]) => v >= a && v <= b);
  const all = rs => values.length >= 2 && values.every(v => inSet(v, rs));
  if (all([[1, 15], [113, 127]])) return 'rel-twos';
  // 65 だけが続く場合は「64 中心 +1」とも「符号ビット -1」とも取れる。実機で多い 64 中心（Pioneer DDJ など）を優先し、
  // 1〜15 と 65〜79 の両方が来たときだけ符号ビット方式とする。違っていたら設定画面で方式を切り替えられる
  if (all([[1, 15], [65, 79]]) && values.some(v => v >= 65) && values.some(v => v <= 15)) return 'rel-sign';
  if (all([[49, 63], [65, 79]]) && new Set(values).size <= 6) return 'rel-offset';
  return 'abs';
}

export class Midi {
  constructor({ flash, onPorts } = {}) {
    this.flash = flash ?? (() => {});
    this.onPorts = onPorts ?? (() => {});
    this.entries = [];
    this.name = 'default';
    this.learning = null;
    this.state = new Map();          // 割り当てごとの一時状態（ソフトテイクオーバー・14bit の MSB・ボタンの押下）
    this.ws = null;
    actions.onState(name => this.led(name));
  }

  async init() {
    try {
      const r = await fetch(`/api/mappings/${this.name}`);
      this.entries = (await r.json()).entries ?? [];
    } catch { this.entries = []; }
    this.connect();
    this.pollPorts();
    return this;
  }

  connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = e => { const m = JSON.parse(e.data); if (m.type === 'midi') this.handle(m.port, m.data); };
    ws.onopen = () => { for (const n of new Set(this.entries.map(x => x.action))) this.led(n); };
    ws.onclose = () => setTimeout(() => this.connect(), 2000);
    this.ws = ws;
  }

  async pollPorts() {
    try { this.onPorts(await (await fetch('/api/midi/ports')).json()); } catch { /* サーバー再起動中など */ }
    setTimeout(() => this.pollPorts(), 3000);
  }

  save() {
    fetch(`/api/mappings/${this.name}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify({ entries: this.entries }) }).catch(() => {});
  }

  key(e) { return `${e.port}|${e.type}|${e.ch}|${e.num}`; }

  handle(port, data) {
    const ev = parse(port, data);
    if (!ev) return;
    if (this.learning) { this.feedLearn(ev); return; }
    for (const m of this.entries) {
      if ((m.port !== '*' && m.port !== ev.port) || m.ch !== ev.ch) continue;
      if (m.type === ev.type && m.num === ev.num) this.apply(m, ev);
      else if (m.lsb != null && ev.type === 'cc' && ev.num === m.lsb) this.applyLsb(m, ev);
    }
  }

  st(m) {
    const k = this.key(m) + '>' + m.action;
    if (!this.state.has(k)) this.state.set(k, {});
    return this.state.get(k);
  }

  apply(m, ev) {
    const a = actions.get(m.action);
    if (!a) return;
    const s = this.st(m);
    if (m.mode === 'button') {
      const down = ev.value > 63 || (ev.type === 'note' && ev.value > 0);
      if (down === !!s.down) return;           // CC ボタンの押しっぱなしの再送は無視
      s.down = down;
      actions.trigger(m.action, down);
      return;
    }
    if (m.mode.startsWith('rel')) {
      const d = decodeRel(m.mode, ev.value);
      if (a.type === 'absolute' && a.get) actions.set(m.action, Math.max(0, Math.min(1, a.get() + d * 0.01)));
      else actions.step(m.action, d);
      return;
    }
    // 絶対値
    let v;
    if (ev.type === 'pb') v = ev.value / 16383;
    else if (m.lsb != null) { s.msb = ev.value; v = ((ev.value << 7) | (s.lsb ?? 0)) / 16383; }
    else v = ev.value / 127;
    this.setAbs(m, a, v);
  }

  applyLsb(m, ev) {
    const s = this.st(m);
    s.lsb = ev.value;
    if (s.msb == null) return;
    const a = actions.get(m.action);
    if (a) this.setAbs(m, a, ((s.msb << 7) | s.lsb) / 16383);
  }

  /** ソフトテイクオーバー: ハードの値が画面の値に近づくか、画面の値をまたいだら追従を始める。 */
  setAbs(m, a, v) {
    const s = this.st(m);
    const cur = a.get ? a.get() : v;
    if (s.picked && s.lastSet != null && Math.abs(cur - s.lastSet) > 0.02) s.picked = false;   // 画面側で動かされた
    if (!s.picked) {
      const crossed = s.lastHw != null && (s.lastHw - cur) * (v - cur) <= 0;
      if (Math.abs(v - cur) < PICKUP || crossed) s.picked = true;
    }
    s.lastHw = v;
    if (!s.picked) return;
    actions.set(m.action, v);
    s.lastSet = v;
  }

  /** MIDI Learn を始める。次に動かした操作子を action に割り当てる。Esc で中止。 */
  learn(action, onDone) {
    this.cancelLearn();
    const a = actions.get(action);
    this.learning = { action, kind: a?.type ?? 'trigger', events: [], timer: null, onDone };
    this.flash(`MIDI 割り当て: コントローラーの操作子を動かしてください（Esc で中止）`, 8000);
    this.escHandler = e => { if (e.key === 'Escape') { this.cancelLearn(); this.flash('MIDI 割り当てを中止しました'); } };
    addEventListener('keydown', this.escHandler);
  }

  cancelLearn() {
    if (!this.learning) return;
    clearTimeout(this.learning.timer);
    this.learning.onDone?.(null);
    this.learning = null;
    removeEventListener('keydown', this.escHandler);
  }

  feedLearn(ev) {
    const L = this.learning;
    L.events.push(ev);
    if (L.events.length === 1) {
      if (ev.type === 'note' || ev.type === 'pb') { this.finishLearn(); return; }
      L.timer = setTimeout(() => this.finishLearn(), LEARN_WAIT);
    }
  }

  finishLearn() {
    const L = this.learning;
    if (!L) return;
    const first = L.events[0];
    let entry;
    if (first.type === 'note') {
      entry = { port: first.port, type: 'note', ch: first.ch, num: first.num, action: L.action, mode: 'button' };
    } else if (first.type === 'pb') {
      entry = { port: first.port, type: 'pb', ch: first.ch, num: 0, action: L.action, mode: 'abs' };
    } else {
      const same = L.events.filter(e => e.type === 'cc' && e.ch === first.ch && e.num === first.num);
      const lsb = L.events.some(e => e.type === 'cc' && e.ch === first.ch && e.num === first.num + 32) && first.num < 32
        ? first.num + 32 : undefined;
      const isButton = L.kind === 'trigger' || L.kind === 'toggle';
      const mode = isButton ? 'button' : classifyCc(same.map(e => e.value));
      entry = { port: first.port, type: 'cc', ch: first.ch, num: first.num, action: L.action, mode, ...(lsb && mode === 'abs' ? { lsb } : {}) };
    }
    // 同じ操作子・同じアクションの古い割り当ては置き換える
    this.entries = this.entries.filter(e => !(this.key(e) === this.key(entry)) && e.action !== L.action);
    this.entries.push(entry);
    this.save();
    const done = L.onDone;
    this.learning = null;
    removeEventListener('keydown', this.escHandler);
    const what = entry.type === 'note' ? `ノート ${entry.num}` : entry.type === 'pb' ? 'ピッチベンド' : `CC ${entry.num}`;
    this.flash(`割り当てました: ${entry.port} ch${entry.ch + 1} ${what}（${modeLabel(entry.mode)}）→ ${L.action}`);
    done?.(entry);
    this.led(L.action);
  }

  unmap(action) {
    const n = this.entries.length;
    this.entries = this.entries.filter(e => e.action !== action);
    if (this.entries.length !== n) { this.save(); this.flash(`割り当てを外しました: ${action}`); }
  }

  mapped(action) { return this.entries.filter(e => e.action === action); }

  /** トグル/ボタンの状態をコントローラーの LED に送る。 */
  led(name) {
    if (this.ws?.readyState !== 1) return;
    const on = actions.state(name);
    if (on === undefined) return;
    for (const m of this.entries) {
      if (m.action !== name || m.mode !== 'button') continue;
      const st = (m.type === 'note' ? 0x90 : 0xb0) | m.ch;
      this.ws.send(JSON.stringify({ type: 'midi_out', port: m.port, data: [st, m.num, on ? 127 : 0] }));
    }
  }
}

export const modeLabel = m => ({ button: 'ボタン', abs: '絶対値', 'rel-twos': '相対値(2の補数)', 'rel-offset': '相対値(64中心)', 'rel-sign': '相対値(符号ビット)' }[m] ?? m);
