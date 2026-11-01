// ミックスキューのデータと操作（design_phase1.md §5.3）。表示は ui/queue-view.js。
// 変更は少し待ってまとめてサーバーに保存する（作業中のキュー "__current__"）。
import { api } from './api.js';
import { renderQueue } from './ui/queue-view.js';
import { joinOf, TEMPLATES } from './engine/transition.js';

let items = [];        // [{track_id, use_start, use_end, use_label, track}]
let saveTimer = null;
const listeners = new Set();

/** 再生状態（P1-4 以降でデッキから設定）。{A: track_id|null, B: track_id|null, played: Set} */
export const playState = { A: null, B: null, played: new Set() };

export const getItems = () => items;
export const onChange = fn => listeners.add(fn);

function statusOf(it, i, nextIdx) {
  if (playState.A === it.track_id) return 'onA';
  if (playState.B === it.track_id) return 'onB';
  if (playState.played.has(it.track_id)) return 'done';
  const an = it.track.analysis_status;
  if (an === 'running') return 'running';
  if (an === 'failed') return 'failed';
  if (an !== 'done') return 'wait';
  return i === nextIdx ? 'next' : '';
}

/** 次にかける曲: 再生中・再生済みより後ろで最初の曲。 */
export function nextIndex() {
  let last = -1;
  items.forEach((it, i) => {
    if (playState.A === it.track_id || playState.B === it.track_id || playState.played.has(it.track_id)) last = i;
  });
  return last + 1 < items.length ? last + 1 : -1;
}

const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const SHORT = { long: 'ロングブレンド', short: 'ショート', bass: '低音入れ替え', filter: 'フィルター', echo: 'エコーアウト', cut: 'カットイン' };

/** 使う範囲の表示（名前があれば名前、範囲を指定していれば時刻、無ければ「フル」）。 */
export function useLabel(it) {
  const ranged = it.use_start != null || it.use_end != null;
  const range = ranged ? `${fmt(it.use_start ?? 0)}–${fmt(it.use_end ?? it.track.duration ?? 0)}` : 'フル';
  return it.use_label ? `${it.use_label}（${range}）` : range;
}

let editing = { item: -1, join: -1 };     // つなぎ目タブで開いている曲・つなぎ（キューの表示で強調する）
export function setEditing(e) { editing = { item: -1, join: -1, ...e }; render(); }

function render() {
  const ni = nextIndex();
  renderQueue(items.map((it, i) => {
    const j = joinOf(it);
    return {
      track: it.track, status: statusOf(it, i, ni), use: useLabel(it), editing: editing.item === i,
      how: { label: j.tpl === 'cut' ? 'カットイン' : `${SHORT[j.tpl]} ${j.bars}小節${j.params.tempo === 'ramp' ? '・ランプ' : ''}${j.params.loop ? `・↻${j.params.loop}` : ''}${j.params.shift ? `・${j.params.shift > 0 ? '+' : ''}${j.params.shift}半音` : ''}`,
             current: editing.join === i },
    };
  }));
  listeners.forEach(fn => fn(items));
}

function changed() {
  render();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api.putQueue(items.map(({ track, ...rest }) => rest)).catch(console.error), 400);
}

/** 待たずにすぐサーバーへ保存する（ミックスとして残す・書き出しの記録の前に。Phase 4）。 */
export async function flush() {
  clearTimeout(saveTimer);
  await api.putQueue(items.map(({ track, ...rest }) => rest));
}

export async function loadQueue() {
  items = (await api.getQueue()).items;
  render();
}

export function add(track) {
  items.push({ track_id: track.id, use_start: null, use_end: null, use_label: null, track });
  changed();
}

export function remove(index) { items.splice(index, 1); changed(); }

/** index の位置に曲を入れる（その曲の次に合う曲を、すぐ後ろに入れるときに使う）。 */
export function insert(index, track) {
  items.splice(Math.max(0, Math.min(items.length, index)), 0, { track_id: track.id, use_start: null, use_end: null, use_label: null, track });
  changed();
}

/** おまかせの案をキューに入れる（Phase 3）。mode: 'replace'（置き換える）/ 'append'（後ろに足す）。
 *  list: [{track_id, track, use_start, use_end, use_label, join_bars, join_tpl, join_params}]。つなぎは次の曲の id と一緒に保存する。 */
export function setAll(list, mode = 'replace') {
  const add = list.map(x => ({ use_label: null, join_params: null, ...x }));
  items = mode === 'append' ? [...items, ...add] : add;
  items.forEach((it, i) => { if (it.join_tpl) it.join_next = items[i + 1]?.track_id ?? null; });
  changed();
}

/** キューの 1 曲の設定（使う範囲・名前・次の曲へのつなぎ）を変える。つなぎは次の曲の id と一緒に保存する。 */
export function patch(index, fields) {
  const it = items[index];
  if (!it) return;
  Object.assign(it, fields);
  if (Object.keys(fields).some(k => k.startsWith('join_'))) it.join_next = items[index + 1]?.track_id ?? null;
  changed();
}

export function move(from, to) {
  if (from === to || from < 0 || from >= items.length) return;
  const [it] = items.splice(from, 1);
  items.splice(Math.max(0, Math.min(items.length, to)), 0, it);
  changed();
}

/** ライブラリ側で曲の情報（解析状態など）が変わったときに反映する。 */
export function updateTrack(track) {
  let hit = false;
  for (const it of items) if (it.track_id === track.id) { it.track = { ...it.track, ...track }; hit = true; }
  if (hit) render();
}

export async function saveAs(name) { await api.putQueue(items.map(({ track, ...rest }) => rest), name); }

export const refresh = render;

/** キュー一覧のドラッグ並べ替え・削除ボタン。 */
export function initQueueInteractions({ onOpenItem, onOpenJoin, onMatch } = {}) {
  const list = document.getElementById('qlist');
  // 曲をダブルクリック → つなぎ目タブでその曲の範囲、曲間（↓）をクリック → そのつなぎを編集
  list.addEventListener('dblclick', e => {
    const qi = e.target.closest('.qi');
    if (qi && !e.target.closest('[data-qdel]') && !e.target.closest('[data-qmatch]')) onOpenItem?.(+qi.dataset.idx);
  });
  list.addEventListener('click', e => {
    const tr = e.target.closest('.qtr[data-join]');
    if (tr) onOpenJoin?.(+tr.dataset.join);
    const mt = e.target.closest('[data-qmatch]');
    if (mt) onMatch?.(+mt.dataset.qmatch);
  });
  let dragFrom = -1;
  list.addEventListener('dragstart', e => {
    const qi = e.target.closest('.qi');
    if (!qi) return;
    dragFrom = +qi.dataset.idx;
    e.dataTransfer.effectAllowed = 'move';
    qi.classList.add('dragging');
  });
  list.addEventListener('dragover', e => {
    if (dragFrom < 0) return;
    e.preventDefault();
    const qi = e.target.closest('.qi');
    list.querySelectorAll('.qi.dropbefore, .qi.dropafter').forEach(x => x.classList.remove('dropbefore', 'dropafter'));
    if (qi) {
      const r = qi.getBoundingClientRect();
      qi.classList.add(e.clientY < r.top + r.height / 2 ? 'dropbefore' : 'dropafter');
    }
  });
  list.addEventListener('drop', e => {
    e.preventDefault();
    const qi = e.target.closest('.qi');
    if (qi && dragFrom >= 0) {
      let to = +qi.dataset.idx + (qi.classList.contains('dropafter') ? 1 : 0);
      if (to > dragFrom) to -= 1;
      move(dragFrom, to);
    }
  });
  list.addEventListener('dragend', () => {
    dragFrom = -1;
    list.querySelectorAll('.qi').forEach(x => x.classList.remove('dragging', 'dropbefore', 'dropafter'));
  });
  list.addEventListener('click', e => {
    const del = e.target.closest('[data-qdel]');
    if (del) remove(+del.dataset.qdel);
  });
}
