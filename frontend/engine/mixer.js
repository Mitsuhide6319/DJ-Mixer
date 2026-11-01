// ミキサー（design_phase1.md §4.1）。チャンネルごとに
//   トリム（ラウドネス補正 × GAIN）→ 3 バンド EQ（アイソレーター）→ フィルター → メーター分岐 → チャンネルフェーダー → クロスフェーダー → マスター
//
// EQ は 250Hz / 2.5kHz の Linkwitz-Riley 4 次（2 次 Butterworth の 2 段）で 3 帯域に分け、各帯域の音量を変えて足し戻す。
// LR4 の LP+HP は 2 次オールパスになるので、全帯域に同じ位相を与えるよう補正すると、全部 0dB のとき振幅特性が平坦になる:
//   低 = LP250² · AP2500   中 = HP250² · LP2500²   高 = HP250² · HP2500²
//   低+中+高 = (LP250² + HP250²)·AP2500 = AP250 · AP2500（振幅は 1）
// つまみ左端で帯域を完全に消せる（ベーススワップ用、打合せ1）。
const XO_LOW = 250, XO_HIGH = 2500;
// Web Audio の lowpass/highpass の Q は dB 指定（Butterworth の Q=0.7071 → -3.01dB）。allpass の Q は通常の値。
// これを取り違えるとクロスオーバー周波数で +7.5dB 盛り上がる（P1-5 の測定で判明）
const Q_BUTTER_DB = 20 * Math.log10(Math.SQRT1_2), Q_BUTTER = Math.SQRT1_2;
const SMOOTH = 0.012;      // パラメータ変更のなめらかさ（ジッパーノイズ防止）

function biquad(ctx, type, f) {
  const b = ctx.createBiquadFilter();
  b.type = type;
  b.frequency.value = f;
  b.Q.value = type === 'allpass' ? Q_BUTTER : Q_BUTTER_DB;
  return b;
}
function chain(nodes) { for (let i = 0; i < nodes.length - 1; i++) nodes[i].connect(nodes[i + 1]); return nodes; }

/** EQ つまみ 0..1 → 倍率。0.5 で 0dB、1.0 で +6dB、0 で完全に消える（0.25 で約 -12dB）。 */
export const eqGain = v => (v >= 0.5 ? Math.pow(10, ((v - 0.5) / 0.5) * 6 / 20) : Math.pow(v / 0.5, 2));
/** GAIN つまみ 0..1 → 倍率（±12dB）。 */
export const trimGain = v => Math.pow(10, (v - 0.5) * 24 / 20);
/** チャンネルフェーダー 0..1 → 倍率。 */
export const faderGain = v => v * v;

/** クロスフェーダー x（0=A 側、1=B 側）とカーブ → [A の倍率, B の倍率]。 */
export function xfadeGains(x, curve) {
  if (curve === 'linear') return [1 - x, x];
  if (curve === 'cut') {       // 端の近くで急に切れる（真ん中は両方とも全開）
    const w = 0.06;
    return [Math.min(1, (1 - x) / w), Math.min(1, x / w)];
  }
  return [Math.cos(x * Math.PI / 2), Math.sin(x * Math.PI / 2)];   // なめらか（等パワー）
}

class Channel {
  constructor(ctx, dest) {
    this.ctx = ctx;
    this.input = ctx.createGain();          // トリム
    // カットインの音量合わせ（design_cutin.md ③）。時刻を指定して掛け、次の曲の 16 小節かけて 1 へ戻す
    this.joinGain = ctx.createGain();
    this.input.connect(this.joinGain);
    const lo = chain([biquad(ctx, 'lowpass', XO_LOW), biquad(ctx, 'lowpass', XO_LOW), biquad(ctx, 'allpass', XO_HIGH)]);
    const mid = chain([biquad(ctx, 'highpass', XO_LOW), biquad(ctx, 'highpass', XO_LOW),
                       biquad(ctx, 'lowpass', XO_HIGH), biquad(ctx, 'lowpass', XO_HIGH)]);
    const hi = chain([biquad(ctx, 'highpass', XO_LOW), biquad(ctx, 'highpass', XO_LOW),
                      biquad(ctx, 'highpass', XO_HIGH), biquad(ctx, 'highpass', XO_HIGH)]);
    this.bands = { low: ctx.createGain(), mid: ctx.createGain(), high: ctx.createGain() };
    const sum = ctx.createGain();
    for (const [first, last, g] of [[lo[0], lo.at(-1), this.bands.low], [mid[0], mid.at(-1), this.bands.mid],
                                    [hi[0], hi.at(-1), this.bands.high]]) {
      this.joinGain.connect(first);
      last.connect(g).connect(sum);
    }
    // フィルター: ローパスとハイパスを直列にし、使わない側は可聴域の外に置く（種類の切り替えによるクリックを避ける）
    this.lp = ctx.createBiquadFilter(); this.lp.type = 'lowpass'; this.lp.frequency.value = 22000; this.lp.Q.value = 0.9;
    this.hp = ctx.createBiquadFilter(); this.hp.type = 'highpass'; this.hp.frequency.value = 10; this.hp.Q.value = 0.9;
    this.meter = ctx.createAnalyser();
    this.meter.fftSize = 1024;
    this.fader = ctx.createGain();
    this.xfade = ctx.createGain();
    sum.connect(this.lp).connect(this.hp);
    this.hp.connect(this.meter);             // メーターはフェーダー前（チャンネルの音量を見る）
    this.hp.connect(this.fader).connect(this.xfade).connect(dest);
    // エコー（design_phase2.md §5）: フェーダーの前から分岐し、マスターへ直接戻す。フェーダーを下げても余韻が残る
    this.echoSend = ctx.createGain(); this.echoSend.gain.value = 0;
    this.echoDelay = ctx.createDelay(4);
    this.echoFb = ctx.createGain(); this.echoFb.gain.value = 0.55;
    this.echoLp = ctx.createBiquadFilter(); this.echoLp.type = 'lowpass'; this.echoLp.frequency.value = 6000;   // 繰り返すごとに少し丸く
    this.hp.connect(this.echoSend).connect(this.echoDelay);
    this.echoDelay.connect(this.echoLp).connect(this.echoFb).connect(this.echoDelay);
    this.echoLp.connect(dest);
    this.loudnessGain = 1;
    this.trim = 0.5;
    this.set = { gain: v => { this.trim = v; this.applyTrim(); } };
  }

  ramp(param, v) { param.setTargetAtTime(v, this.ctx.currentTime, SMOOTH); }
  applyTrim() { this.ramp(this.input.gain, this.loudnessGain * trimGain(this.trim)); }
  setLoudness(lufs, target) {
    // 上げすぎは +9dB まで（マスターのリミッターでも保護する）
    const db = Math.max(-12, Math.min(9, target - lufs));
    this.loudnessGain = Math.pow(10, db / 20);
    this.applyTrim();
    return db;
  }
  setEq(band, v) { this.ramp(this.bands[band].gain, eqGain(v)); }
  setFilter(v) {
    // 中央 ±0.03 は素通し。左でローパス（22kHz→80Hz）、右でハイパス（10Hz→10kHz）、周波数は指数的に動かす
    const dz = 0.03;
    const lpF = v < 0.5 - dz ? 80 * Math.pow(22000 / 80, (v) / (0.5 - dz)) : 22000;
    const hpF = v > 0.5 + dz ? 10 * Math.pow(10000 / 10, (v - 0.5 - dz) / (0.5 - dz)) : 10;
    this.ramp(this.lp.frequency, lpF);
    this.ramp(this.hp.frequency, hpF);
  }
  setFader(v) { this.ramp(this.fader.gain, faderGain(v)); }
  /** エコーの送り量（0..1）と遅延（秒。拍に合わせて 3/4 拍を渡す）。 */
  setEcho(send, delaySec) {
    if (delaySec) this.echoDelay.delayTime.setTargetAtTime(Math.min(3.9, delaySec), this.ctx.currentTime, 0.02);
    this.ramp(this.echoSend.gain, Math.max(0, Math.min(1, send)));
  }
  setXfade(g) { this.ramp(this.xfade.gain, g); }
  /** 出力時刻 at にクロスフェーダーの倍率を g にする（カットインの切り替え。ほぼ瞬時、クリックしない程度の 2ms）。 */
  setXfadeAt(g, at) { this.xfade.gain.setTargetAtTime(g, at, 0.002); }
  /** 出力時刻 at から音量を db にし、backSec かけて 0dB へ戻す。 */
  setJoinGainAt(db, at, backSec) {
    const p = this.joinGain.gain;
    p.cancelScheduledValues(at);
    p.setValueAtTime(Math.pow(10, db / 20), at);
    if (backSec > 0) p.linearRampToValueAtTime(1, at + backSec);
  }
  resetJoinGain() {
    const p = this.joinGain.gain, t = this.ctx.currentTime;
    p.cancelScheduledValues(t);
    p.setTargetAtTime(1, t, SMOOTH);
  }
}

export class Mixer {
  constructor(ctx, dest) {
    this.ctx = ctx;
    this.ch = { A: new Channel(ctx, dest), B: new Channel(ctx, dest) };
    this.x = 0.5;
    this.curve = 'smooth';
    this.targetLufs = -14;
    this.applyXfade();
  }
  setCrossfader(x) { this.x = Math.max(0, Math.min(1, x)); this.applyXfade(); }
  setCurve(c) { this.curve = c; this.applyXfade(); }
  /** 出力時刻 at にクロスフェーダーを x の倍率へ切り替える予約（つまみの位置 this.x は変えない。後で runner が合わせる）。 */
  scheduleXfade(x, at) {
    const [a, b] = xfadeGains(x, this.curve);
    this.ch.A.setXfadeAt(a, at);
    this.ch.B.setXfadeAt(b, at);
  }
  applyXfade() {
    const [a, b] = xfadeGains(this.x, this.curve);
    this.ch.A.setXfade(a);
    this.ch.B.setXfade(b);
  }
}
