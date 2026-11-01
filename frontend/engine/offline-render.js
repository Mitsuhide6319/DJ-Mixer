// 高速書き出しのストレッチを、再生と同じ Signalsmith Stretch（WebAssembly）で音を出さずに計算する（2026-10-02）。
//
// サーバーの書き出し（djmix/export/render.py）は操作の表（512 サンプルの刻みごとの入力位置）を作り、デッキごと・曲ごとの区間を
// ここに頼む。区間ごとに OfflineAudioContext を作り、ストレッチのノードへ入力位置と速度を予約して（再生の SYNC と同じ schedule）、
// 実時間より速く描き、planar float32 でサーバーへ送る。python-stretch は呼び出しごとに独立して処理し、区切りで音量が落ちていた。
import SignalsmithStretch from '../vendor/signalsmith-stretch/SignalsmithStretch.mjs';

const MARGIN = 1.5;            // 入力の読み込みの前後の余白（秒。ストレッチの遅れの分も含む）
const EVERY = 2;               // 何刻みごとに予約するか（2 刻み ≈ 23ms。再生の SYNC は 20ms ごと）
const PCM_CHUNK = 44100 * 30;

async function fetchPcm(seg, a, b) {
  const sr = seg.sr, chans = Array.from({ length: seg.channels }, () => []);
  let off = Math.max(0, Math.floor(a * sr));
  const end = Math.ceil(b * sr);
  while (off < end) {
    const frames = Math.min(PCM_CHUNK, end - off);
    const url = seg.kind === 'stems' ? `/api/tracks/${seg.track_id}/stems-pcm?offset=${off}&frames=${frames}`
      : `/api/tracks/${seg.track_id}/pcm?offset=${off}&frames=${frames}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error(`曲の音を読めませんでした（${r.status}）`);
    const buf = await r.arrayBuffer();
    const n = buf.byteLength / 4 / seg.channels;
    if (!n) break;
    for (let c = 0; c < seg.channels; c++) chans[c].push(new Float32Array(buf.slice(c * n * 4, (c + 1) * n * 4)));
    off += n;
  }
  return { start: Math.max(0, Math.floor(a * sr)) / sr, chunks: chans };
}

/** 1 つの区間を計算して、planar float32（channels × 刻み数 × step）を返す。 */
export async function renderSegment(seg) {
  const N = seg.pos.length, step = seg.step, sr = seg.sr;
  const frames = N * step;
  const ctx = new OfflineAudioContext(seg.channels, frames, sr);
  ctx.destination.channelCountMode = 'explicit';
  ctx.destination.channelInterpretation = 'discrete';
  const node = await SignalsmithStretch(ctx, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [seg.channels] });
  node.channelInterpretation = 'discrete';
  node.connect(ctx.destination);
  let lo = Infinity, hi = -Infinity;
  for (const p of seg.pos) { if (p < lo) lo = p; if (p > hi) hi = p; }
  const pcm = await fetchPcm(seg, lo - MARGIN, hi + MARGIN);
  for (let k = 0; k < pcm.chunks[0].length; k++) await node.addBuffers(pcm.chunks.map(ch => ch[k]));
  // 予約: 刻み k の出力時刻に入力位置 pos[k] を鳴らす。間は次の予約までの速度で進む（adjustPrevious で位置をぴったり合わせる）
  const P = seg.pos, base = pcm.start, dt = step / sr;
  let last = null;
  for (let k = 0; k < N; k += EVERY) {
    const k2 = Math.min(N - 1, k + EVERY);
    let rate = k2 > k ? (P[k2] - P[k]) / ((k2 - k) * dt) : 1;
    const jump = k > 0 && (P[k] < P[k - EVERY] - 1e-3 || Math.abs(P[k] - P[Math.max(0, k - EVERY)]) > 4 * EVERY * dt);   // ループの折り返し
    if (rate < 0 || rate > 4) rate = 1;                                   // 折り返しをまたぐ予約は、次の予約で位置を合わせ直す
    last = node.schedule({ output: k * dt, input: P[k] - base, rate, semitones: seg.shift || 0, active: true }, k > 0 && !jump);
  }
  await last;                                                             // 予約がすべて届いてから描く
  const out = await ctx.startRendering();
  const body = new Float32Array(2 * frames);
  if (seg.kind === 'stems') {
    // パートの音: 刻みごとのパートの音量（render.py の pg。刻みの間は直線）を掛けて 2ch に混ぜる（送る量が 1/4 になる）
    const G = seg.pg;
    for (let s = 0; s < 4; s++) {
      const L = out.getChannelData(2 * s), R = out.getChannelData(2 * s + 1);
      for (let k = 0; k < N; k++) {
        const g0 = G[k][s], g1 = (G[k + 1] ?? G[k])[s];
        if (g0 === 0 && g1 === 0) continue;
        for (let j = 0, i = k * step; j < step; j++, i++) {
          const g = g0 + (g1 - g0) * j / step;
          body[i] += L[i] * g;
          body[frames + i] += R[i] * g;
        }
      }
    }
  } else {
    body.set(out.getChannelData(0).subarray(0, frames), 0);
    body.set(out.getChannelData(1).subarray(0, frames), frames);
  }
  return body;
}

/** 区間ごとの計算と送信にかかった時間（調べるとき用）。 */
export const stats = [];
const PARALLEL = 2;            // 同時に計算する区間の数（描くのは同時でも速くならないが、1 つを送っている間に次を描ける）

/** サーバーの書き出しが頼んでいる区間をすべて計算して送る。onStep(i, n) で進み具合。 */
export async function renderAll(onStep, cancelled = () => false) {
  const { segments } = await fetch('/api/export/segments').then(r => r.json());
  const todo = segments.filter(s => !s.done).sort((a, b) => b.pos.length * b.channels - a.pos.length * a.channels);   // 重いものから
  let i = segments.length - todo.length, failed = null;
  const worker = async () => {
    while (todo.length && !failed && !cancelled()) {
      const seg = todo.shift();
      try {
        const t0 = performance.now();
        const body = await renderSegment(seg);
        const t1 = performance.now();
        const r = await fetch(`/api/export/segments/${seg.id}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `送れませんでした（${r.status}）`);
        stats.push({ id: seg.id, kind: seg.kind, sec: Math.round(seg.pos.length * seg.step / seg.sr), renderMs: Math.round(t1 - t0), sendMs: Math.round(performance.now() - t1) });
        onStep?.(++i, segments.length);
      } catch (e) { failed = e; }
    }
  };
  await Promise.all(Array.from({ length: PARALLEL }, worker));
  if (failed) throw failed;
}
