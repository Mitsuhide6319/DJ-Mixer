// ミックスキューの表示（design_phase1.md §5.3）。データ操作（追加・並べ替え・保存）は P1-2 で実装する。
import { starsHTML } from './stars.js';

/** 倍/半テンポも考慮して、b2 を b1 に合わせるのに必要な変化率(%)と倍率 k。 */
export function tempoRel(b1, b2) {
  let best = null;
  for (const k of [1, 2, 0.5]) {
    const pct = (b1 / (b2 * k) - 1) * 100;
    if (!best || Math.abs(pct) < Math.abs(best.pct)) best = { pct, k };
  }
  return best;
}

/** Camelot 表記どうしの関係 [記号, 色クラス, 説明]。 */
export function keyRel(k1, k2) {
  const n1 = parseInt(k1, 10), n2 = parseInt(k2, 10), l1 = k1.slice(-1), l2 = k2.slice(-1);
  const dn = Math.min((n1 - n2 + 12) % 12, (n2 - n1 + 12) % 12);
  if (dn === 0 && l1 === l2) return ['◎', 'ok', '同じキー'];
  if ((dn === 1 && l1 === l2) || (dn === 0 && l1 !== l2)) return ['○', 'ok', '隣接キー'];
  if (dn === 2 && l1 === l2) return ['△', 'mid', '2つ隣'];
  return ['×', 'ng', '衝突の可能性'];
}

const fmt = s => { s = Math.max(0, s); return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const CHIP = {
  onA: '<span class="chip a">A 再生中</span>', onB: '<span class="chip b">B 再生中</span>',
  next: '<span class="chip next">次</span>', done: '<span class="chip">再生済み</span>',
  wait: '<span class="chip">解析待ち</span>',
  running: '<span class="chip next">解析中…</span>', failed: '<span class="chip" style="color:var(--active)">解析失敗</span>',
};

function item(q, i) {
  const t = q.track, cls = t.beat_class ?? 'N';
  return `<div class="qi ${q.status ?? ''} ${q.editing ? 'cur' : ''}" data-idx="${i}" draggable="true"><div class="bar"></div><div class="no">${i + 1}</div>
    <div><div class="t1">${esc(t.title)}${CHIP[q.status] ?? ''}</div><div class="t2">${esc(t.artist ?? '')} ・ ${esc(q.use ?? 'フル')} ${starsHTML(t)}</div>${t.album ? `<div class="t3" title="${esc(t.album)}">『${esc(t.album)}』</div>` : ''}</div>
    <div class="meta">${t.bpm ? t.bpm.toFixed(1) : '—'} <span>BPM</span><br>${t.camelot ?? '—'} <span>${fmt(t.duration ?? 0)}</span>
      <span class="cls c${cls}" style="margin-left:2px">${cls === 'N' ? '·' : cls}</span></div>
    <div class="qdel" data-qdel="${i}" title="キューから外す">×</div>
    ${t.analysis_status === 'done' ? `<div class="qmatch" data-qmatch="${i}" title="この曲の次に合う曲を探して、すぐ後ろに入れます">合う曲</div>` : ''}</div>`;
}

function transition(a, b, how, i) {
  if (!a.track.bpm || !b.track.bpm) return `<div class="qtr" data-join="${i}"><span class="arr">↓</span>解析後に相性を表示</div>`;
  const t = tempoRel(a.track.bpm, b.track.bpm), k = keyRel(a.track.camelot, b.track.camelot);
  const tc = Math.abs(t.pct) <= 3 ? 'ok' : Math.abs(t.pct) <= 8 ? 'mid' : 'ng';
  return `<div class="qtr ${how?.current ? 'cur' : ''}" data-join="${i}" title="クリックでこのつなぎを編集"><span class="arr">↓</span>
    <span class="${tc}">テンポ ${t.pct >= 0 ? '+' : ''}${t.pct.toFixed(1)}%${t.k !== 1 ? ` (×${t.k})` : ''}</span>
    <span class="${k[1]}" title="${k[2]}">キー ${k[0]}</span><span class="how">${esc(how?.label ?? '未設定（つなぎ目で編集）')}</span></div>`;
}

/** queue: [{track:{title,artist,bpm,camelot,duration,beat_class}, status, use, how}] */
export function renderQueue(queue) {
  const list = document.getElementById('qlist');
  if (!queue.length) {
    list.innerHTML = '<div class="qempty">キューは空です。<br>ライブラリの「＋キュー」で曲を追加してください。</div>';
    document.getElementById('qsum').textContent = '0曲';
    document.getElementById('qnext').innerHTML = 'キュー: 空';
    return;
  }
  let html = '';
  queue.forEach((q, i) => {
    html += item(q, i);
    if (i < queue.length - 1) html += transition(q, queue[i + 1], q.how, i);
  });
  list.innerHTML = html;
  const total = queue.reduce((s, q) => s + (q.track.duration ?? 0), 0);
  document.getElementById('qsum').textContent = `${queue.length}曲 ・ 約${Math.round(total / 60)}分`;
  const ni = queue.findIndex(q => q.status === 'next');
  const next = queue[ni];
  document.getElementById('qnext').innerHTML = next
    ? `キュー 次: <b>${esc(next.track.title)}</b><span>${next.track.bpm?.toFixed(1) ?? '—'} BPM ・ ${next.track.camelot ?? '—'}</span><span>残り${queue.length - ni}曲</span>`
    : 'キュー: 次の曲なし';
}
