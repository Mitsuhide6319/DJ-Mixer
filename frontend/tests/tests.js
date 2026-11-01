// ブラウザ単体テスト。ビルド不要。/tests/test.html から実行する。
import { Mixer, eqGain, xfadeGains, trimGain } from '../engine/mixer.js';
import { tempoRel, keyRel } from '../ui/queue-view.js';
import { parse, decodeRel, classifyCc } from '../input/midi.js';
import { levels, joinOf, overlapStart, lowKnob, judge, inStartBar, entersAtEnd, autoTrimDb, trimDb } from '../engine/transition.js';
import { BeatMap } from '../engine/beatmap.js';

const results = [];
async function test(name, fn) {
  try { await fn(); results.push([true, name]); } catch (e) { results.push([false, name, e.message]); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const near = (a, b, tol, what) => assert(Math.abs(a - b) <= tol, `${what}: ${a.toFixed(2)} ≠ ${b}±${tol}`);

/** チャンネル A の周波数特性（dB）を OfflineAudioContext のインパルス応答から求める。 */
async function response(setup, freqs) {
  const sr = 44100, N = 65536, n = N + 22050;
  const off = new OfflineAudioContext(1, n, sr);
  const m = new Mixer(off, off.destination);
  m.setCrossfader(0);
  setup(m.ch.A);
  const b = off.createBuffer(1, n, sr);
  b.getChannelData(0)[0] = 1;
  const s = off.createBufferSource();
  s.buffer = b;
  s.connect(m.ch.A.input);
  s.start(0.3);                       // つまみの変化（12ms のなめらか化）が終わってから通す
  const out = (await off.startRendering()).getChannelData(0).subarray(Math.round(0.3 * sr) - 64);
  const r = {};
  for (const f of freqs) {
    let re = 0, im = 0;
    for (let i = 0; i < N; i++) { const w = 2 * Math.PI * f * i / sr; re += out[i] * Math.cos(w); im -= out[i] * Math.sin(w); }
    r[f] = 20 * Math.log10(Math.hypot(re, im) + 1e-12);
  }
  return r;
}

await test('EQ つまみの倍率: 中央 0dB・右端 +6dB・左端 0・1/4 で約 -12dB', () => {
  near(eqGain(0.5), 1, 1e-9, 'center');
  near(20 * Math.log10(eqGain(1)), 6, 1e-9, 'max');
  assert(eqGain(0) === 0, 'kill');
  near(20 * Math.log10(eqGain(0.25)), -12, 0.1, 'quarter');
  near(20 * Math.log10(trimGain(1)), 12, 1e-9, 'trim max');
});

await test('クロスフェーダー: なめらか=等パワー、カット=中央で両方全開', () => {
  const [a, b] = xfadeGains(0.5, 'smooth');
  near(a * a + b * b, 1, 1e-9, 'power');
  assert(xfadeGains(0, 'smooth')[1] < 1e-9 && xfadeGains(1, 'smooth')[0] < 1e-9, 'ends');
  const [c1, c2] = xfadeGains(0.5, 'cut');
  assert(c1 === 1 && c2 === 1, 'cut center');
  assert(xfadeGains(0, 'cut')[1] === 0, 'cut end');
});

const F = [60, 120, 250, 1000, 2500, 5000, 10000];
await test('アイソレーター: 全部 0dB のとき 60Hz〜10kHz が ±0.3dB で平坦', async () => {
  const r = await response(() => {}, F);
  for (const f of F) near(r[f], 0, 0.3, `${f}Hz`);
});
await test('アイソレーター: LOW を消すと 60Hz が -40dB 以下、1kHz は ±0.3dB', async () => {
  const r = await response(c => c.setEq('low', 0), F);
  assert(r[60] < -40, `60Hz ${r[60].toFixed(1)}`);
  near(r[1000], 0, 0.3, '1kHz');
});
await test('アイソレーター: MID を消すと 1kHz が -25dB 以下', async () => {
  const r = await response(c => c.setEq('mid', 0), F);
  assert(r[1000] < -25, `1kHz ${r[1000].toFixed(1)}`);
  near(r[60], 0, 0.3, '60Hz');
  near(r[10000], 0, 0.3, '10kHz');
});
await test('アイソレーター: HIGH を消すと 10kHz が -40dB 以下', async () => {
  const r = await response(c => c.setEq('high', 0), F);
  assert(r[10000] < -40, `10kHz ${r[10000].toFixed(1)}`);
  near(r[250], 0, 0.3, '250Hz');
});
await test('フィルター: 左でローパス、右でハイパス、中央は素通し', async () => {
  const lp = await response(c => c.setFilter(0.2), F);
  assert(lp[10000] < -30 && Math.abs(lp[120]) < 1, 'lowpass');
  const hp = await response(c => c.setFilter(0.8), F);
  assert(hp[60] < -30 && Math.abs(hp[10000]) < 1, 'highpass');
  const c = await response(c => c.setFilter(0.51), F);
  for (const f of F) near(c[f], 0, 0.3, `center ${f}Hz`);
});

await test('相性表示: テンポ差は倍/半も考慮、Camelot の関係', () => {
  const t = tempoRel(140, 72);
  assert(t.k === 2 && Math.abs(t.pct + 2.78) < 0.05, `tempo ${JSON.stringify(t)}`);
  assert(keyRel('8A', '8A')[0] === '◎' && keyRel('8A', '9A')[0] === '○' && keyRel('8A', '8B')[0] === '○', 'key near');
  assert(keyRel('12A', '1A')[0] === '○' && keyRel('8A', '10A')[0] === '△' && keyRel('8A', '3B')[0] === '×', 'key far');
});

await test('MIDI: メッセージの解釈（ノート・ノートオフ・CC・ピッチベンド）', () => {
  assert(JSON.stringify(parse('p', [0x91, 36, 100])) === JSON.stringify({ port: 'p', type: 'note', ch: 1, num: 36, value: 100 }), 'note on');
  assert(parse('p', [0x80, 36, 64]).value === 0, 'note off');
  assert(parse('p', [0xb0, 7, 90]).type === 'cc', 'cc');
  assert(parse('p', [0xe0, 0, 64]).value === 8192, 'pitch bend center');
});
await test('MIDI: 相対値の 3 方式の復号', () => {
  assert(decodeRel('rel-twos', 1) === 1 && decodeRel('rel-twos', 127) === -1, 'twos');
  assert(decodeRel('rel-offset', 65) === 1 && decodeRel('rel-offset', 63) === -1, 'offset');
  assert(decodeRel('rel-sign', 1) === 1 && decodeRel('rel-sign', 65) === -1, 'sign');
});
await test('MIDI Learn: エンコーダーとフェーダーの見分け', () => {
  assert(classifyCc([60, 62]) === 'abs', '中央付近をゆっくり動かしたフェーダー');
  assert(classifyCc([1, 2, 3]) === 'abs', '下端付近をゆっくり動かしたフェーダー');
  assert(classifyCc([1, 1, 1]) === 'rel-twos' && classifyCc([127, 127, 1]) === 'rel-twos', '2 の補数');
  assert(classifyCc([65, 65, 65]) === 'rel-offset' && classifyCc([63, 63, 65]) === 'rel-offset', '64 中心');
  assert(classifyCc([1, 1, 65]) === 'rel-sign', '符号ビット');
});

await test('繋ぎ: ロングブレンドは入れ替え位置で低音が入れ替わり、クロスフェーダーは端から端へ', () => {
  const j = joinOf({ join_tpl: 'long', join_bars: 16 });
  const beats = 64;
  const s = levels(j, 0, beats), m1 = levels(j, 0.45, beats), m2 = levels(j, 0.55, beats), e = levels(j, 1, beats);
  assert(s.xf === 0 && e.xf === 1 && Math.abs(levels(j, 0.5, beats).xf - 0.5) < 1e-9, 'xf');
  assert(m1.lowOut === 1 && m1.lowIn === 0 && m2.lowOut === 0 && m2.lowIn === 1, 'swap');
  const j2 = joinOf({ join_tpl: 'long', join_params: { swapAt: 0.25, xfStart: 0.2, xfEnd: 0.6 } });
  assert(levels(j2, 0.3, beats).lowIn === 1 && levels(j2, 0.2, beats).lowIn === 0, 'swapAt');
  assert(levels(j2, 0.1, beats).xf === 0 && levels(j2, 0.7, beats).xf === 1, 'xf range');
});
await test('繋ぎ: エコーアウト・フィルター・低音だけ・カット', () => {
  const echo = joinOf({ join_tpl: 'echo' });
  assert(echo.bars === 2, 'echo bars');
  assert(levels(echo, 0.2, 8).echoOut === 0 && levels(echo, 0.99, 8).echoOut > 0.9 && levels(echo, 0.99, 8).xf === 1, 'echo');
  const f = joinOf({ join_tpl: 'filter' });
  assert(levels(f, 0, 64).hpOut === 0 && levels(f, 1, 64).hpOut > 0.8 && levels(f, 1, 64).xf > 0.999, 'filter');   // (1-0.8)/0.2 は浮動小数点で 1 未満になる
  const b = joinOf({ join_tpl: 'bass' });
  assert(levels(b, 0.5, 64).xf === 0.5 && levels(b, 1, 64).xf === 1, 'bass');
  assert(joinOf({ join_tpl: 'cut', join_bars: 16 }).bars === 0, 'cut has no overlap');
  assert(joinOf({ join_tpl: 'bogus' }).tpl === 'long', 'unknown template falls back');
  assert(lowKnob(1) === 0.5 && lowKnob(0) === 0, 'knob');
});
await test('繋ぎ: 重なりの始まりは、使う範囲の終わりから N 小節前の小節頭', () => {
  const bars = Array.from({ length: 40 }, (_, i) => i * 2);
  const r = overlapStart(bars, 60.3, 16);
  assert(r.endBar === 30 && r.startBar === 14 && r.startTime === 28, JSON.stringify(r));
});

await test('繋ぎ: 拍を合わせられるかの判定（テンポ差・倍/半・拍なし区間）と次の曲の入り口', () => {
  // 一定テンポの合成の拍: bpm、長さ sec、拍なし区間 noBeat
  const bm = (bpm, sec = 200, noBeat = []) => {
    const beats = Array.from({ length: Math.floor(sec * bpm / 60) }, (_, i) => 0.3 + i * 60 / bpm);
    return new BeatMap({ beats, meter: 4, bars: beats.filter((_, i) => i % 4 === 0).map((t, i) => ({ beat: i * 4, time: t, conf: 1 })), no_beat: noBeat });
  };
  const a = bm(120);
  let r = judge(a, bm(125), 100, 130, 0.3);
  assert(r.k === 1 && Math.abs(r.rate - 0.96) < 1e-6 && r.reason === null, 'near tempo: ' + JSON.stringify(r));
  r = judge(a, bm(62), 100, 130, 0.3);
  assert(r.k === 0.5 && r.reason === null, 'half-tempo track follows at k=1/2: ' + JSON.stringify(r));
  r = judge(a, bm(90), 100, 130, 0.3);
  assert(r.reason?.includes('テンポの差'), 'too far: ' + JSON.stringify(r));        // 1.33 倍・0.67 倍のどちらも範囲外
  r = judge(bm(120, 200, [[110, 115]]), bm(120), 100, 130, 0.3);
  assert(r.reason?.includes('前の曲'), 'no-beat in out: ' + JSON.stringify(r));
  r = judge(a, bm(120, 200, [[0, 5]]), 100, 130, 0.3);
  assert(r.reason?.includes('次の曲'), 'no-beat in in: ' + JSON.stringify(r));
  assert(inStartBar([0.69, 2.8, 4.9, 7.0], 0) === 0 && inStartBar([0.69, 2.8, 4.9, 7.0], 4.0) === 2, 'nearest bar');
  assert(entersAtEnd('echo') && entersAtEnd('cut') && !entersAtEnd('long'), 'late entry');
});

await test('繋ぎ: 高速書き出し（Python 版）と同じ値（levels・joinOf）', async () => {
  const fx = await (await fetch('/tests/levels_fixture.json')).json();
  let bad = 0;
  for (const c of fx.cases) {
    const j = joinOf(c.item);
    if (j.tpl !== c.join.tpl || j.bars !== c.join.bars) bad++;
    const o = levels(j, c.u, c.beats);
    for (const k of ['xf', 'lowOut', 'lowIn', 'hpOut', 'echoOut']) if (Math.abs(o[k] - c.out[k]) > 1e-9) bad++;
  }
  assert(bad === 0, `${bad} 件の値が違う（${fx.cases.length} 件中）`);
});

await test('エコー: チャンネルフェーダーを下げても余韻が遅延ごとに残る（前から分岐）', async () => {
  const sr = 44100, n = sr * 2;
  const off = new OfflineAudioContext(1, n, sr);
  const m = new Mixer(off, off.destination);
  m.setCrossfader(0);
  m.ch.A.setFader(0);                       // フェーダーを下げた状態
  m.ch.A.setEcho(1, 0.3);
  const b = off.createBuffer(1, n, sr); for (let i = 0; i < 400; i++) b.getChannelData(0)[i] = Math.sin(i / 5);
  const s = off.createBufferSource(); s.buffer = b; s.connect(m.ch.A.input); s.start(0.1);
  const out = (await off.startRendering()).getChannelData(0);
  const peak = (a, z) => { let v = 0; for (let i = Math.round(a * sr); i < Math.round(z * sr); i++) v = Math.max(v, Math.abs(out[i])); return v; };
  assert(peak(0.1, 0.35) < 1e-3, 'dry is muted by the fader');
  assert(peak(0.39, 0.43) > 0.05, 'first echo at +0.3s');
  assert(peak(0.69, 0.73) > 0.02 && peak(0.69, 0.73) < peak(0.39, 0.43), 'second echo decays');
});

// カットイン（design_cutin.md）: 音量合わせは cutpoints.py と同じ値（tests/test_cutin.py と同じ例）
await test('カットイン: 音量合わせ（Python 版と同じ値）', () => {
  const bars = (db, n = 60, bl = 2) => Array.from({ length: n }, (_, i) => ({ t: i * bl, db }));
  assert(autoTrimDb(bars(-10), 80, 2, -14, bars(-20), 0, 2, -14, -14) === 6, 'clamp +6');
  assert(autoTrimDb(bars(-10), 80, 2, -14, bars(-13), 0, 2, -14, -14) === 3, '+3');
  assert(autoTrimDb(bars(-10), 80, 2, -14, bars(-10.5), 0, 2, -14, -14) === 0, 'dead zone');
  assert(autoTrimDb(bars(-10), 80, 2, -14, bars(-13), 0, 2, -18, -14) === -1, 'with loudness');
  assert(trimDb({ cutTrim: 'off' }, 5) === 0 && trimDb({ cutTrim: -2.5 }, 5) === -2.5 && trimDb({ cutTrim: 20 }, 5) === 6 && trimDb({}, 5) === 5, 'trimDb');
});
await test('カットイン: クロスフェーダーの予約は指定した時刻で切り替わる（±1ms）', async () => {
  const sr = 44100, n = Math.round(0.4 * sr), off = new OfflineAudioContext(1, n, sr);
  const m = new Mixer(off, off.destination);
  m.setCrossfader(0);
  // 1kHz の正弦波（チャンネルのハイパス 10Hz は直流を通さない）。A は 0.25、B は 0.8 の振幅
  const src = id => { const o = off.createOscillator(); o.frequency.value = 1000; const g = off.createGain(); g.gain.value = id === 'A' ? 0.25 : 0.8; o.connect(g).connect(m.ch[id].input); o.start(0); };
  src('A'); src('B');
  m.scheduleXfade(1, 0.2);
  m.ch.B.setJoinGainAt(-6, 0.2, 0.1);
  const y = (await off.startRendering()).getChannelData(0);
  const pk = t => { let v = 0; for (let i = Math.round(t * sr); i < Math.round((t + 0.002) * sr); i++) v = Math.max(v, Math.abs(y[i])); return v; };
  near(pk(0.19), 0.25, 0.02, 'before: A only');
  const g0 = Math.pow(10, -6 / 20);
  near(pk(0.215), 0.8 * (g0 + (1 - g0) * 0.16), 0.02, 'after: B with -6dB (returning over 0.1s)');
  near(pk(0.35), 0.8, 0.02, 'trim returns to 0dB');
  let cross = 0;
  for (let t = 0.15; t < 0.3; t += 0.0005) if (Math.abs(pk(t) - 0.25) > 0.03) { cross = t; break; }
  near(cross, 0.2, 0.0025, 'switch time');
});

const ok = results.every(r => r[0]);
document.getElementById('out').innerHTML = results.map(([p, n, m]) =>
  `<span class="${p ? 'ok' : 'ng'}">${p ? '✓' : '✗'} ${n}${m ? ' — ' + m : ''}</span>`).join('\n') +
  `\n\n${results.filter(r => r[0]).length}/${results.length} 成功`;
window.testResult = { ok, results };
