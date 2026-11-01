// 次の曲の候補リスト（案Y。design_phase3.md §4.4）。モックアップ mockup/phase3/ui_p3_Y.html の候補カード。
// おまかせの案の曲の差し替えと、ライブラリの「この曲に合う曲」で使う。
import { openModal, closeModal } from './modal.js';

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const TPL = { long: 'ロングブレンド', short: 'ショート', echo: 'エコーアウト' };

/**
 * o: {title, prev, next, groups, exclude, colors, previewTrack(track, use), fullTrack(id), onPick(c), pickLabel}
 * prev だけ: prev の次に合う曲。prev と next: 差し替え（前後の相性の平均）。
 */
export function openCandidates(o) {
  const color = s => s > 0.8 ? o.colors['--ok'] : s > 0.65 ? o.colors['--warn'] : o.colors['--active'];
  const parts = j => [['テンポ', j.tempo], ['キー', j.key], ['響き', j.chroma], ['雰囲気', j.mood]]
    .map(([n, v]) => `<span class="pt"><i>${n}</i><b style="width:${Math.round(v * 44)}px;background:${color(v)}"></b></span>`).join('');
  // 絞り込み（ジャンルのまとまり・アーティスト）。ジャンルの初期値は呼び出し元の範囲（おまかせの条件）。前回の絞り込みは覚えておく
  let groups = [...(o.groups ?? [])], artists = [];
  try { const last = JSON.parse(sessionStorage.getItem('djmix.cand') || 'null'); if (last && !o.groups?.length) { groups = last.groups ?? []; artists = last.artists ?? []; } } catch { /* 無くてよい */ }
  let facets = { artists: [], groups: [] };
  const m = openModal(`<h3>${esc(o.title)}</h3>
    <div class="candf">
      <div class="cfrow"><span class="lbl2">ジャンル</span><div class="cfchips" data-g></div></div>
      <div class="cfrow"><span class="lbl2">アーティスト</span><div class="cfchips" data-a></div>
        <input data-ain list="cand-artists" placeholder="名前を入れて Enter（解析済みの曲のアーティスト）"><datalist id="cand-artists"></datalist></div>
      <div class="lbl2" data-hint>${o.next ? '前の曲・次の曲の両方との相性で並べています' : '相性の良い順'}。棒は相性の内訳（テンポ・キー・繋ぐ部分の響き・音の雰囲気）</div>
    </div>
    <div class="cands" data-list><div class="lbl2" style="padding:12px">探しています…</div></div>
    <div class="mfoot"><div class="btn" data-close>閉じる</div></div>`, null);
  m.querySelector('.mbox').classList.add('wide');

  async function load() {
    renderFilters();
    try { sessionStorage.setItem('djmix.cand', JSON.stringify({ groups, artists })); } catch { /* 無くてよい */ }
    const qs = new URLSearchParams({ n: artists.length ? 20 : 10, groups: groups.join(','), exclude: (o.exclude ?? []).join(',') });
    artists.forEach(a => qs.append('artist', a));
    if (o.prev != null) qs.set('prev', o.prev);
    if (o.next != null) qs.set('next', o.next);
    let items = [];
    try { items = (await fetch(`/api/auto/next?${qs}`).then(r => r.json())).items; } catch { /* 下で表示 */ }
    const el = m.querySelector('[data-list]');
    if (!items.length) {
      el.innerHTML = `<div class="lbl2" style="padding:12px;line-height:1.8">候補がありません。${artists.length || groups.length ? '絞り込みを外すか広げてください（テンポが ±30% 以内の曲だけが候補です）。' : 'ライブラリの解析を進めてください。'}</div>`;
      return;
    }
    el.innerHTML = items.map((c, i) => {
      const j = c.join ?? c.join_next, de = c.energy_delta ?? 0;
      return `<div class="cand" data-i="${i}"><div class="h"><b>${esc(c.title)}</b><span>${esc((c.artist ?? '').slice(0, 36))}${c.album ? ` ・ 『${esc(c.album.slice(0, 40))}』` : ''}</span>
          <span class="sc" style="color:${color(c.score)}">${c.score.toFixed(2)}</span></div>
        <div class="row"><span class="mono dim">${c.bpm.toFixed(1)} BPM ・ ${c.key}</span>
          ${c.energy_delta != null ? `<span style="color:${de > 0.05 ? '#fb923c' : de < -0.05 ? '#2dd4bf' : 'var(--dim)'}">${de > 0.05 ? '▲ 盛り上がる' : de < -0.05 ? '▼ 落ち着く' : '― 同じくらい'}</span>` : ''}
          <span class="sp" style="flex:1"></span><span class="lbl2">${j ? TPL[j.tpl] ?? '' : ''}</span>
          <span class="btn" data-pv>▶ 試聴</span><span class="btn primary" data-pick>${esc(o.pickLabel ?? 'これにする')}</span></div>
        ${j ? `<div class="parts">${parts(j)}</div><div class="w">${esc(j.why.join(' ／ '))}</div>` : ''}</div>`;
    }).join('');
    el.querySelectorAll('.cand').forEach(card => {
      const c = items[+card.dataset.i];
      card.querySelector('[data-pv]').addEventListener('click', async () => o.previewTrack(await o.fullTrack(c.id), c.use, 'head'));
      card.querySelector('[data-pick]').addEventListener('click', async e => {
        if ((await o.onPick(c)) !== false) { closeModal(); return; }
        e.target.textContent = '✓ 足しました'; e.target.classList.remove('primary'); e.target.style.pointerEvents = 'none';
      });
    });
  }
  // ---- 絞り込みの表示と操作 ----
  function renderFilters() {
    const gl = facets.groups.length ? facets.groups : groups.map(name => ({ name, count: null }));
    m.querySelector('[data-g]').innerHTML = `<span class="cf ${groups.length ? '' : 'on'}" data-gall>すべて</span>` +
      gl.map(g => `<span class="cf ${groups.includes(g.name) ? 'on' : ''}" data-gv="${esc(g.name)}">${esc(g.name)}${g.count != null ? `<small>${g.count.toLocaleString()}</small>` : ''}</span>`).join('');
    m.querySelector('[data-a]').innerHTML = artists.length
      ? artists.map(a => `<span class="cf on" data-av="${esc(a)}">${esc(a)}<b title="外す">✕</b></span>`).join('') + '<span class="cf" data-aall>すべて</span>'
      : '<span class="lbl2">すべて</span>';
  }
  m.querySelector('[data-g]').addEventListener('click', e => {
    if (e.target.closest('[data-gall]')) groups = [];
    else { const v = e.target.closest('[data-gv]')?.dataset.gv; if (v == null) return; groups = groups.includes(v) ? groups.filter(x => x !== v) : [...groups, v]; }
    load();
  });
  m.querySelector('[data-a]').addEventListener('click', e => {
    if (e.target.closest('[data-aall]')) artists = [];
    else { const v = e.target.closest('[data-av]')?.dataset.av; if (v == null) return; artists = artists.filter(x => x !== v); }
    load();
  });
  const ain = m.querySelector('[data-ain]');
  const addArtist = () => {
    const v = ain.value.trim();
    if (!v) return;
    const hit = facets.artists.find(a => a.name.toLowerCase() === v.toLowerCase());
    if (!hit && facets.artists.length) { ain.select(); m.querySelector('[data-hint]').textContent = `「${v}」の解析済みの曲はありません（候補から選んでください）`; return; }
    const name = hit?.name ?? v;
    if (!artists.includes(name)) artists = [...artists, name];
    ain.value = '';
    load();
  };
  ain.addEventListener('keydown', e => { if (e.key === 'Enter') addArtist(); });
  ain.addEventListener('change', () => { if (facets.artists.some(a => a.name === ain.value)) addArtist(); });     // 候補をクリックで選んだとき
  fetch('/api/auto/facets').then(r => r.json()).then(f => {
    facets = f;
    m.querySelector('#cand-artists').innerHTML = f.artists.map(a => `<option value="${esc(a.name)}">${a.count} 曲</option>`).join('');
    renderFilters();
  }).catch(() => {});
  load();
}
