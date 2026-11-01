// 曲の評価（★1〜5。Phase 4。design_phase4.md §1.4）。どの画面の ★ も同じ部品で、クリックで保存し、同じ曲の ★ をすべて更新する。
// 同じ ★ をもう一度押すと評価を消す。
// Phase 5 E: アプリで付けた ★ が無い曲は iTunes の ★ を出す（ピンク）。アプリの ★ を消すと iTunes の ★ に戻る。
const cache = new Map();          // track_id → {app: アプリの ★, it: iTunes の ★}
const listeners = new Set();

const shown = id => { const c = cache.get(id); return c ? (c.app || c.it || 0) : 0; };
const isIt = id => { const c = cache.get(id); return !!c && !c.app && !!c.it; };
export const ratingOf = shown;
export const onRating = fn => listeners.add(fn);

/** 一覧などで受け取った曲の ★ を覚える。rating_app / rating_it があればそれ、無ければ rating をアプリの ★ として。 */
function learn(t) {
  if (t?.id == null) return;
  if (t.rating_app !== undefined) cache.set(t.id, { app: t.rating_app ?? 0, it: t.rating_it ?? (t.rating_src === 'itunes' ? t.rating : 0) });
  else if (t.rating != null && !cache.has(t.id)) cache.set(t.id, t.rating_src === 'itunes' ? { app: 0, it: t.rating } : { app: t.rating, it: 0 });
}
export function remember(tracks) { for (const t of tracks ?? []) learn(t); }

/** ★ の HTML。track: {id, rating?, rating_app?, rating_it?}。 */
export function starsHTML(track) {
  if (track?.id == null) return '';
  learn(track);
  const n = shown(track.id), it = isIt(track.id);
  return `<span class="stars ${it ? 'it' : ''}" data-rate="${track.id}" title="${it ? 'iTunes の評価（クリックでアプリの評価を付ける）' : '評価（同じ★をもう一度で消す）'}">` +
    [1, 2, 3, 4, 5].map(k => `<b class="${k <= n ? 'on' : ''}" data-k="${k}">★</b>`).join('') + '</span>';
}

function paint(id) {
  const n = shown(id), it = isIt(id);
  document.querySelectorAll(`.stars[data-rate="${id}"]`).forEach(s => {
    s.classList.toggle('it', it);
    s.querySelectorAll('b').forEach(b => b.classList.toggle('on', +b.dataset.k <= n));
  });
}

document.addEventListener('click', async e => {
  const b = e.target.closest('.stars[data-rate] b');
  if (!b) return;
  e.stopPropagation();
  const id = +b.closest('.stars').dataset.rate, k = +b.dataset.k;
  const c = cache.get(id) ?? { app: 0, it: 0 };
  const next = c.app === k ? 0 : k;
  cache.set(id, { ...c, app: next });
  paint(id);
  try {
    await fetch(`/api/tracks/${id}/rating`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating: next }) });
  } catch { /* 次に読み込んだときに正しい値へ戻る */ }
  listeners.forEach(fn => fn(id, shown(id)));
}, true);
