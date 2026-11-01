// 同期 E2E 試験の録音側（design_phase1.md §7.1）。アプリの画面でコンソールから使う:
//   const t = await import('/tests/sync-e2e.js'); await t.start(); … 数分後 … await t.stop()
// チャンネル A/B の音を左右に分けて録音し、/api/dev/sync-check で拍のずれを測る。
import { engine, sync } from '../app.js';

let rec = null, chunks = [], nodes = [];

/** tap: 'pre'（音量補正の直後・EQ の前。繋ぎの試験用。EQ で低音を消しても拍を測れる）/ 'post'（フェーダーの前・EQ の後） */
export function start(tap = 'pre') {
  const ctx = engine.ctx;
  const merger = ctx.createChannelMerger(2);
  for (const [i, id] of [[0, 'A'], [1, 'B']]) {
    const g = ctx.createGain();
    g.channelCount = 1;
    g.channelCountMode = 'explicit';     // ステレオをモノラルにまとめてから左右に振り分ける
    g.channelInterpretation = 'speakers';
    (tap === 'pre' ? engine.mixer.ch[id].input : engine.mixer.ch[id].hp).connect(g);
    g.connect(merger, 0, i);
    nodes.push(g);
  }
  const dest = ctx.createMediaStreamDestination();
  merger.connect(dest);
  nodes.push(merger);
  chunks = [];
  rec = new MediaRecorder(dest.stream, { mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 256000 });
  rec.ondataavailable = e => chunks.push(e.data);
  rec.start(1000);
  return 'recording';
}

export async function stop(k) {
  await new Promise(res => { rec.onstop = res; rec.stop(); });
  nodes.forEach(n => n.disconnect());
  nodes = [];
  const blob = new Blob(chunks, { type: 'audio/webm' });
  window.__lastRecording = blob;       // 評価をやり直せるよう残しておく
  const kk = k ?? sync.st.B.k;
  const r = await fetch(`/api/dev/sync-check?k=${kk}`, { method: 'POST', body: blob });
  return r.json();
}

/** 直前の録音を評価し直す。windows: [[開始秒, 終了秒], …]（録音の中で比べる区間。ミックスの重なり） */
export async function recheck(k = 1, windows = null) {
  const w = windows ? `&windows=${windows.map(([a, b]) => `${a}-${b}`).join(',')}` : '';
  const r = await fetch(`/api/dev/sync-check?k=${k}${w}`, { method: 'POST', body: window.__lastRecording });
  return r.json();
}
