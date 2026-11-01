// AudioContext とマスター段（design_phase1.md §4.1）。
// デッキ → チャンネル（P1-5 でトリム・EQ・フィルター・フェーダー・クロスフェーダーを入れる）→ マスター → リミッター → 出力。
import SignalsmithStretch from '../vendor/signalsmith-stretch/SignalsmithStretch.mjs';
import { Deck } from './deck.js';
import { Mixer } from './mixer.js';
import { SAMPLE_RATE } from './constants.js';

export { SAMPLE_RATE };

export class AudioEngine {
  constructor() {
    this.ctx = new AudioContext({ sampleRate: SAMPLE_RATE, latencyHint: 'interactive' });
    const c = this.ctx;
    this.master = c.createGain();
    this.limiter = c.createDynamicsCompressor();   // 上げすぎの保護（ラウドネス補正で +5dB 程度まで上げるため）
    this.limiter.threshold.value = -1.5;
    this.limiter.knee.value = 0;
    this.limiter.ratio.value = 20;
    this.limiter.attack.value = 0.002;
    this.limiter.release.value = 0.12;
    this.masterMeter = c.createAnalyser();
    this.masterMeter.fftSize = 1024;
    this.master.connect(this.limiter).connect(this.masterMeter).connect(c.destination);
    this.mixer = new Mixer(c, this.master);
    this.channelIn = { A: this.mixer.ch.A.input, B: this.mixer.ch.B.input };
    this.decks = {};
  }

  async init() {
    for (const id of ['A', 'B']) {
      const node = await SignalsmithStretch(this.ctx);
      this.decks[id] = new Deck(this, id, node, this.channelIn[id]);
    }
    return this;
  }

  /** パートごとに鳴らす用のストレッチ（8ch = 4 パート × ステレオ。Phase 5 B2）。使うときに作る。 */
  createStemNode() {
    return SignalsmithStretch(this.ctx, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [8] });
  }

  /** 自動再生制限で止まっていれば再開する（専用ウィンドウでは制限を外しているので通常は不要）。 */
  async resume() { if (this.ctx.state !== 'running') await this.ctx.resume(); }

  get now() { return this.ctx.currentTime; }
}

/** AnalyserNode のピーク値（0..1）。 */
export function peakOf(analyser, buf) {
  analyser.getFloatTimeDomainData(buf);
  let m = 0;
  for (let i = 0; i < buf.length; i++) { const v = Math.abs(buf[i]); if (v > m) m = v; }
  return m;
}
