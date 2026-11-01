// 解析の依頼と完了の検知（design_phase1.md §3.1: 解析は「使う曲だけ」）。
// 依頼中の曲があるあいだだけ 1 秒ごとに状態を確認し、終わった曲の情報を購読者に配る。
import { api } from './api.js';

export const PRIO = { DECK: 0, QUEUE: 1, MANUAL: 2 };
const listeners = new Set();
const waiting = new Set();
let timer = null;
let since = Date.now() / 1000;

export const onTrackUpdated = fn => listeners.add(fn);

export async function request(trackId, priority = PRIO.MANUAL) {
  const r = await api.analyze(trackId, priority);
  if (r.status !== 'done') {
    waiting.add(trackId);
    poll();
  }
  return r.status;
}

async function poll() {
  if (timer) return;
  timer = setTimeout(async () => {
    timer = null;
    let st;
    try { st = await api.analysisStatus(); } catch { poll(); return; }
    document.getElementById('anq').textContent = String(st.pending + (st.running ? 1 : 0));
    const fresh = st.recent.filter(r => r.t > since).map(r => r.id);
    // 待っている曲の状態（解析中など）も更新するため、終わった曲＋待っている曲をまとめて取り直す
    const ids = [...new Set([...fresh, ...waiting])];
    if (ids.length) {
      const { items } = await api.tracks(ids);
      for (const t of items) {
        if (t.analysis_status === 'done' || t.analysis_status === 'failed') waiting.delete(t.id);
        listeners.forEach(fn => fn(t));
      }
    }
    if (st.recent.length) since = Math.max(since, ...st.recent.map(r => r.t));
    if (waiting.size || st.pending || st.running) poll();
  }, 1000);
}
