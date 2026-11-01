// 1 デッキ（design_phase1.md §4.3〜4.4）。Signalsmith Stretch のノードに曲を流し込み、schedule() で駆動する。
//
// 再生位置は「アンカー」（出力時刻 outT に入力位置 inT を rate 倍速で鳴らし始めた）から計算する:
//   position(t) = inT + rate * (t - outT)
// schedule({output, input, rate}) は「出力時刻 output に入力位置 input を鳴らす」指定なので、アンカーと 1 対 1 に対応する。
// SYNC（P1-6）は、20ms ごとにこのアンカーを更新し続けることで実現する。
import { SAMPLE_RATE } from './constants.js';

const CHUNK = SAMPLE_RATE * 10;   // 10 秒ずつ流し込む
const STEM_CHUNK = SAMPLE_RATE * 30;   // 分離した音は 30 秒ずつ（8ch）
const PART_FADE = 0.012;               // 元の音 ⇔ パートの切り替えの時定数（秒）
const LOOKAHEAD = 0.05;           // 操作の反映は 50ms 先（ノードの遅延補償が効くよう少し先に予約する）

export class Deck {
  constructor(engine, id, node, output) {
    this.engine = engine;
    this.id = id;
    this.node = node;
    this.output = output;
    // 元の音とパートの音（Phase 5 B2）を切り替えるための音量。ふだんは元の音だけ
    this.mainGain = engine.ctx.createGain();
    node.connect(this.mainGain).connect(output);
    this.stems = null;           // {node, bus, gains[4], seg: {trackId, start(秒), end(秒)}}
    this.track = null;
    this.analysis = null;
    this.wave = null;
    this.duration = 0;
    this.loadedSec = 0;
    this.playing = false;
    this.anchor = { outT: 0, inT: 0 };
    // 予約済みのアンカーの履歴。SYNC は 100ms 先を 20ms ごとに予約するので「今」より未来のアンカーが常に数個あり、
    // 今の位置は「今を含む区間」のアンカーから求める必要がある（ループの折り返しをまたぐときに必須。P1-8 で確認）
    this.anchors = [];
    this.rate = 1;               // 実際の再生速度（テンポフェーダー、SYNC 中はスケジューラが決める）
    this.tempoRange = 0.08;
    this.tempoFader = 0;         // -1..+1（フェーダーの位置。+ が速い）
    this.keylock = true;
    this.cue = 0;
    this.hotcues = [null, null, null, null];
    this.loop = null;            // {start, end, beats, active}（入力時刻・秒）
    this.keyShift = 0;           // 音程のずらし（半音。繋ぎのキー合わせ。Phase 4）
    this.lastSeek = null;        // 直前のシーク {from, to}（SYNC が位相を保つのに使う）
    this.previewing = false;
    this.loadToken = 0;
    this.listeners = new Set();
    node.start();
    node.schedule({ output: 0, active: false });
  }

  on(fn) { this.listeners.add(fn); }
  emit(what) { this.listeners.forEach(fn => fn(what, this)); }

  get ctx() { return this.engine.ctx; }

  /** 出力時刻 t（既定は今）に鳴っている入力位置（秒）。 */
  position(t = this.ctx.currentTime) {
    if (!this.playing) return this.anchor.inT;
    const hs = this.anchors;
    let i = hs.length - 1;
    while (i > 0 && hs[i].outT > t) i--;
    const a = hs[i], nx = hs[i + 1];
    let p;
    if (!a) p = this.anchor.inT;
    else if (t < a.outT) p = a.inT;                                          // 鳴り始める前
    else if (nx?.adjusted) p = a.inT + (nx.inT - a.inT) * (t - a.outT) / (nx.outT - a.outT);   // ノードと同じ補間
    else p = a.inT + a.rate * (t - a.outT);
    const L = this.loop;
    if (L?.active && p >= L.end) p = L.start + ((p - L.start) % (L.end - L.start));   // ノード内蔵のループと同じ折り返し
    return Math.min(p, this.duration || p);
  }

  semitones(rate = this.rate) { return (this.keylock ? 0 : 12 * Math.log2(rate)) + this.keyShift; }

  /** 音程を半音ずらす（テンポは変えない）。曲を読み込み直すと 0 に戻る。 */
  setKeyShift(s) { this.keyShift = s; this.applyRate(this.rate); }

  /** アンカーを (outT, inT, rate) に置き直して予約する。
   *  adjust=true（SYNC の連続予約）のときは、直前の区間の速度をノード側で調整して入力位置を途切れさせない。 */
  scheduleAt(outT, inT, rate = this.rate, adjust = false) {
    // outT 以降の予約はノード側で置き換わるので、履歴からも消す
    this.anchors = this.playing ? this.anchors.filter(x => x.outT < outT).slice(-16) : [];
    this.anchor = { outT, inT, rate, adjusted: adjust && this.anchors.length > 0 };
    if (this.playing) this.anchors.push(this.anchor);
    this.rate = rate;
    const L = this.loop?.active ? this.loop : null;
    this.node.schedule({ output: outT, active: this.playing, input: inT, rate, semitones: this.semitones(rate),
                         loopStart: L ? L.start : 0, loopEnd: L ? L.end : 0 }, adjust);
    this.scheduleStems(outT, inT, rate, adjust);
  }

  // ---- パートごとに鳴らす（Phase 5 B2。design_phase5.md §B2） ----
  // 繋ぎの区間だけ、分離した音（8ch）をもう 1 つのストレッチに入れ、元の音と同じ予約で鳴らす。
  // 区間の頭（前の曲: 重なりの始まり、全パート 1）で元の音からパートの音へ、区間の終わり（次の曲: 重なりの終わり、全パート 1）で戻す。

  /** パートの音の予約を、元の音の予約に合わせる（入力位置は区間の頭からの秒）。 */
  scheduleStems(outT, inT, rate = this.rate, adjust = false) {
    const S = this.stems;
    if (!S?.seg || S.seg.trackId !== this.track?.id) return;
    const L = this.loop?.active ? this.loop : null;
    S.node.schedule({ output: outT, active: this.playing, input: inT - S.seg.start, rate, semitones: this.semitones(rate),
                      loopStart: L ? L.start - S.seg.start : 0, loopEnd: L ? L.end - S.seg.start : 0 }, adjust);
  }

  async ensureStemChain() {
    if (this.stems) return this.stems;
    const c = this.ctx;
    const node = await this.engine.createStemNode();
    const split = c.createChannelSplitter(8), bus = c.createGain();
    bus.gain.value = 0;
    node.connect(split);
    const gains = [0, 1, 2, 3].map(s => {
      const m = c.createChannelMerger(2), g = c.createGain();
      split.connect(m, 2 * s, 0);
      split.connect(m, 2 * s + 1, 1);
      m.connect(g).connect(bus);
      return g;
    });
    bus.connect(this.output);
    node.start();
    node.schedule({ output: 0, active: false });
    this.stems = { node, bus, gains, seg: null };
    return this.stems;
  }

  /** 分離した音の区間 [start, end]（入力の秒）を読み込む。戻り値: 読み込めたか。 */
  async loadStemSegment(trackId, start, end) {
    const S = await this.ensureStemChain();
    const token = this.loadToken;
    await S.node.dropBuffers();
    S.seg = null;
    let offset = Math.max(0, Math.floor(start * SAMPLE_RATE));
    const stop = Math.ceil(end * SAMPLE_RATE), first = offset;
    while (offset < stop) {
      const r = await fetch(`/api/tracks/${trackId}/stems-pcm?offset=${offset}&frames=${Math.min(STEM_CHUNK, stop - offset)}`);
      if (!r.ok) return false;
      const n = +r.headers.get('X-Frames');
      const buf = await r.arrayBuffer();
      if (token !== this.loadToken || this.track?.id !== trackId) return false;     // 途中で別の曲が読み込まれた
      if (!n) break;
      await S.node.addBuffers([0, 1, 2, 3, 4, 5, 6, 7].map(ch => new Float32Array(buf.slice(ch * n * 4, (ch + 1) * n * 4))));
      offset += n;
    }
    S.seg = { trackId, start: first / SAMPLE_RATE, end: offset / SAMPLE_RATE };
    // 今の予約をパートの音にも入れる（再生中の前の曲は、この後の予約が無いことがあるため）
    const t = this.ctx.currentTime + LOOKAHEAD;
    this.scheduleStems(t, this.playing ? this.position(t) : this.anchor.inT, this.rate);
    return true;
  }

  /** 元の音（false）とパートの音（true）を切り替える。gains: 切り替えた直後のパートの音量。 */
  setPartsMode(on, gains = [1, 1, 1, 1]) {
    const t = this.ctx.currentTime;
    if (on && !this.stems?.seg) on = false;
    if (on) this.setPartGains(gains, true);
    this.mainGain.gain.setTargetAtTime(on ? 0 : 1, t, PART_FADE);
    this.stems?.bus.gain.setTargetAtTime(on ? 1 : 0, t, PART_FADE);
    this.partsOn = on;
  }

  setPartGains(g, now = false) {
    if (!this.stems) return;
    const t = this.ctx.currentTime;
    this.stems.gains.forEach((x, i) => (now ? x.gain.setValueAtTime(g[i], t) : x.gain.setTargetAtTime(g[i], t, PART_FADE)));
  }

  /** パートの音をやめて区間を手放す（メモリを返す）。 */
  async clearStems() {
    this.setPartsMode(false);
    const S = this.stems;
    if (!S?.seg) return;
    S.seg = null;
    S.node.schedule({ output: this.ctx.currentTime + 0.1, active: false });
    await new Promise(r => setTimeout(r, 120));          // 元の音へ戻りきってから
    if (!S.seg) await S.node.dropBuffers();
  }

  async load(track) {
    const token = ++this.loadToken;
    this.pause();
    // 曲情報は await より前に設定する（解析結果の取得と競合しないように）
    this.track = track;
    this.analysis = null;
    this.wave = null;
    this.duration = track.duration || 0;
    this.loadedSec = 0;
    this.anchor = { outT: 0, inT: 0 };
    this.cue = 0;
    this.hotcues = [null, null, null, null];
    this.loop = null;
    this.keyShift = 0;
    this.emit('load');
    if (this.stems?.seg) this.clearStems();
    await this.node.dropBuffers();
    if (token !== this.loadToken) return;
    let offset = 0, total = Infinity;
    while (offset < total) {
      const r = await fetch(`/api/tracks/${track.id}/pcm?offset=${offset}&frames=${CHUNK}`);
      if (!r.ok) throw new Error(`PCM の取得に失敗しました (${r.status})`);
      total = +r.headers.get('X-Total-Frames');
      const buf = await r.arrayBuffer();
      if (token !== this.loadToken) return;          // 読み込み中に別の曲が読み込まれた
      const frames = buf.byteLength / 8;
      // 左右は別々の ArrayBuffer にする。同じ ArrayBuffer を共有すると、次の曲を読み込むときの dropBuffers() が
      // 同じバッファを 2 回 transfer しようとして DataCloneError になり、読み込みが止まる（P2-7 の試験で見つけた）
      const L = new Float32Array(buf.slice(0, frames * 4)), R = new Float32Array(buf.slice(frames * 4, frames * 8));
      await this.node.addBuffers([L, R]);
      if (token !== this.loadToken) return;
      offset += frames;
      this.duration = total / SAMPLE_RATE;
      this.loadedSec = offset / SAMPLE_RATE;
      this.emit('progress');
      if (frames === 0) break;
    }
    this.emit('loaded');
  }

  /** 解析結果が届いたら設定する（拍・小節・波形）。 */
  setAnalysis(analysis, wave) {
    this.analysis = analysis;
    this.wave = wave;
    const e = analysis.edits ?? {};
    if (e.hotcues) this.hotcues = [0, 1, 2, 3].map(i => e.hotcues[i] ?? null);
    if (e.cue_point != null && this.cue === 0) this.cue = e.cue_point;
    else if (this.cue === 0 && analysis.beats?.length) this.cue = analysis.beats[0];   // 既定のキューは最初の拍
    this.emit('analysis');
  }

  /** at: 鳴り始める出力時刻（カットインで前の曲の小節の頭に合わせる。省略すると今すぐ）。 */
  play(at) {
    if (!this.track || this.playing) return;
    const t = Math.max(typeof at === 'number' ? at : 0, this.ctx.currentTime + LOOKAHEAD);
    if (this.anchor.inT >= this.duration - 0.05) this.anchor.inT = 0;
    this.playing = true;
    // SYNC 中のスレーブは、マスターと拍がそろう位置・速度から始める（sync.js の startInput）
    const s = this.syncHook?.(t);
    this.anchors = [];           // 止まっていた間のアンカーは使わない
    this.scheduleAt(t, s ? s.inT : this.anchor.inT, s ? s.rate : this.baseRate());
    this.emit('state');
  }

  pause() {
    if (!this.playing) return;
    const t = this.ctx.currentTime + LOOKAHEAD;
    const p = this.position(t);
    this.playing = false;
    this.scheduleAt(t, p);
    this.emit('state');
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  seek(sec) {
    if (!this.track) return;
    const p = Math.max(0, Math.min(this.duration, sec));
    const t = this.ctx.currentTime + (this.playing ? LOOKAHEAD : 0);
    const from = this.position(t);
    // ループの外へ飛んだらループは解除する
    if (this.loop?.active && (p < this.loop.start - 0.01 || p > this.loop.end + 0.01)) this.loop.active = false;
    this.scheduleAt(t, p);
    this.lastSeek = { from, to: p };
    this.emit('seek');
  }

  /** 拍の中の位置を保ったまま、別の拍へ飛ぶ（CDJ のクオンタイズ。SYNC の位相がそろったままになる）。 */
  jumpQuantized(targetBeatTime, bm) {
    if (!bm) { this.seek(targetBeatTime); return; }
    const t = this.ctx.currentTime + (this.playing ? LOOKAHEAD : 0);
    const p = bm.pos(this.position(t));
    const frac = this.playing ? p - Math.floor(p) : 0;
    this.seek(bm.time(Math.round(bm.pos(targetBeatTime)) + frac));
  }

  /** ループを設定する（start/end は入力時刻）。 */
  setLoop(start, end, beats) {
    this.loop = { start, end, beats, active: true };
    const t = this.ctx.currentTime + LOOKAHEAD;
    let p = this.position(t);
    if (p >= end || p < start) p = start;
    if (this.playing) this.scheduleAt(t, p);
    this.emit('loop');
  }

  exitLoop() {
    if (!this.loop?.active) return;
    const t = this.ctx.currentTime + LOOKAHEAD;
    const p = this.position(t);
    this.loop.active = false;
    if (this.playing) this.scheduleAt(t, p);
    this.emit('loop');
  }

  /** テンポフェーダーから決まる再生速度（SYNC していないとき）。 */
  baseRate() { return 1 + this.tempoFader * this.tempoRange; }

  setTempoFader(v) {             // v: -1..+1
    this.tempoFader = Math.max(-1, Math.min(1, v));
    this.applyRate(this.baseRate());
  }

  setTempoRange(r) { this.tempoRange = r; this.applyRate(this.baseRate()); }

  /** 再生中なら、今の位置から新しい速度で鳴らし直す。 */
  applyRate(rate) {
    if (this.playing) {
      const t = this.ctx.currentTime + LOOKAHEAD;
      this.scheduleAt(t, this.position(t), rate);
    } else {
      this.rate = rate;
    }
    this.emit('tempo');
  }

  setKeylock(on) { this.keylock = on; this.applyRate(this.rate); }

  /** 最も近い拍の時刻（解析前はそのまま）。 */
  snapToBeat(sec) {
    const b = this.analysis?.beats;
    if (!b?.length) return sec;
    let lo = 0, hi = b.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (b[m] <= sec) lo = m; else hi = m; }
    return Math.abs(b[lo] - sec) <= Math.abs(b[hi] - sec) ? b[lo] : b[hi];
  }

  // CUE（CDJ 方式、design_phase1.md §4.4）
  // 再生中に押す → キューに戻って停止。停止中にキュー以外の位置で押す → そこ（最寄りの拍）を新しいキューにする。
  // 停止中にキューの位置で押している間 → 試聴再生し、離すとキューに戻る。
  cueDown() {
    if (!this.track) return;
    if (this.playing) { this.pause(); this.seek(this.cue); return; }
    const p = this.position();
    if (Math.abs(p - this.cue) > 0.02) {
      this.cue = this.snapToBeat(p);
      this.seek(this.cue);
      this.emit('cue');
    } else {
      this.previewing = true;
      this.play();
    }
  }

  cueUp() {
    if (!this.previewing) return;
    this.previewing = false;
    this.pause();
    this.seek(this.cue);
  }

  /** 曲の終わりに来たら止める（描画ループから呼ぶ）。 */
  tick() {
    if (this.playing && this.duration && this.position() >= this.duration - 0.01) {
      this.pause();
      this.emit('ended');
    }
  }
}
