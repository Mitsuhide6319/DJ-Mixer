// ライブラリ画面（design_phase1.md §5.1）。左=絞り込み（すべて/解析済み/フォルダ・アーティスト・アルバム）、右=曲一覧。
// 一覧は 200 曲ずつ、スクロールに合わせて追加で読み込む（1 万曲を一度に描画しない）。
// 並べ替え（I-1）・列の幅（I-4）・左の一覧の種類は設定 lib_view に残す（専用ウィンドウは localStorage が再起動で消えるため）。
import { api } from '../api.js';
import { starsHTML, remember } from './stars.js';
import { putSettings } from './settings-view.js';

const PAGE = 200;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = s => (s ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}` : '—');
/** 最後にかけた日（今日・昨日・n日前・月/日）。 */
function lastPlayed(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000), days = Math.floor((Date.now() - d) / 86400000);
  return days <= 0 ? '今日' : days === 1 ? '昨日' : days < 7 ? `${days}日前` : `${d.getMonth() + 1}/${d.getDate()}`;
}
const AN = { none: '未解析', queued: '解析待ち', running: '解析中', done: '', failed: '解析失敗' };
const UNKNOWN = '（不明）';

// 列: [見出し, キー, 並べ替えできるか, 既定の幅(px)]
const COLS = [['', 'act', false, 200], ['#', 'pos', true, 44], ['曲名', 'title', true, 300], ['アーティスト', 'artist', true, 200], ['アルバム', 'album', true, 220],
  ['長さ', 'duration', true, 64], ['BPM', 'bpm', true, 72], ['キー', 'camelot', true, 62], ['LUFS', 'lufs', false, 64],
  ['★', 'rating', true, 108], ['再生回数', 'plays', true, 80], ['最後にかけた日', 'last_played', true, 124], ['拍', 'cls', false, 120]];
const MIN_W = 40;
const FIRST_DESC = new Set(['rating', 'last_played']);          // ★・最後にかけた日は、最初のクリックで多い順・新しい順

export function initLibraryView({ onAddToQueue, onLoadDeck, isQueued, onMatch }) {
  const tree = document.getElementById('libtree');
  const tbl = document.getElementById('libtbl');
  const box = tbl.parentElement;
  const chips = document.getElementById('libchips');
  const search = document.getElementById('libsearch');
  // 絞り込み: 左の一覧（すべて/解析済み/フォルダ）と、アーティスト・アルバム（I-4）は重ねて使える
  const filter = { q: '', folder: null, root: null, analyzed: null, artist: null, album: null, playlist: null, playlistName: '' };
  let view = { sort: { key: 'path', desc: false }, widths: {}, mode: 'folder' };
  let rows = [], total = 0, loading = false, seq = 0;
  let facetCache = { key: null, items: [] };
  let plSortBefore = null;                // プレイリストを選ぶ前の並べ方
  const facetKey = () => (view.mode === 'album' ? `album:${filter.artist === null ? '*' : '=' + filter.artist}` : view.mode);

  tree.insertAdjacentHTML('beforeend', `
    <div class="fold on" data-f="all">すべての曲 <span id="cntAll">—</span></div>
    <div class="fold" data-f="analyzed">解析済み <span id="cntAn">—</span></div>
    <div class="libmode"><b data-mode="folder">フォルダ</b><b data-mode="artist">アーティスト</b><b data-mode="album">アルバム</b><b data-mode="playlist" title="iTunes のプレイリスト">プレイリスト</b></div>
    <div class="facetfind" id="facetfind"><input placeholder="一覧の中を探す"></div>
    <div id="folders"></div>
    <div class="libstatus" id="libstatus"></div>`);
  const listEl = document.getElementById('folders');
  const findEl = document.querySelector('#facetfind input');

  // ---- 見た目の設定（並べ替え・列の幅・左の一覧の種類）を残す ----
  let saveTimer;
  const saveView = () => {
    clearTimeout(saveTimer);
    // プレイリストの順（#）は、プレイリストを選んでいる間だけの並べ方なので覚えない
    const v = view.sort.key === 'pos' ? { ...view, sort: plSortBefore ?? { key: 'path', desc: false } } : view;
    saveTimer = setTimeout(() => putSettings({ lib_view: v }).catch(() => {}), 400);
  };
  async function loadView() {
    try {
      const s = await fetch('/api/settings').then(r => r.json());
      const v = s.lib_view ?? {};
      view = { ...view, ...v, sort: { ...view.sort, ...(v.sort ?? {}) }, widths: { ...(v.widths ?? {}) } };
      if (!v.sort) {                // 前の版で localStorage に覚えた並べ方を引き継ぐ
        try { const old = JSON.parse(localStorage.getItem('djmix.libsort') || 'null'); if (old?.key) { view.sort = old; saveView(); } } catch { /* 無くてよい */ }
      }
    } catch { /* 既定のまま */ }
  }
  const widthOf = k => Math.max(MIN_W, view.widths[k] ?? COLS.find(c => c[1] === k)[3]);
  const tableWidth = () => COLS.reduce((a, c) => a + widthOf(c[1]), 0);

  const header = () => `<colgroup>${COLS.map(([, k]) => `<col data-c="${k}" style="width:${widthOf(k)}px">`).join('')}</colgroup><tr>` +
    COLS.map(([l, k, s]) => {
      const rsz = `<span class="rsz" data-rsz="${k}" title="ドラッグで幅を変える（ダブルクリックで元の幅）"></span>`;
      if (k === 'cls') return `<th title="A=そのまま拍合わせ可 / B=補正で可 / C=拍合わせ不可">${l}${rsz}</th>`;
      if (!s || (k === 'pos' && filter.playlist === null)) return `<th ${k === 'pos' ? 'title="プレイリストの順"' : ''}>${l}${rsz}</th>`;
      const on = view.sort.key === k || (k === 'pos' && filter.playlist !== null && view.sort.key === 'path');
      return `<th class="sortable ${on ? 'sorted' : ''}" data-sort="${k}" title="クリックで並べ替え">${l}<i>${on ? (view.sort.desc ? '▼' : '▲') : ''}</i>${rsz}</th>`;
    }).join('') + '</tr>';

  function rowHTML(t) {
    const cls = t.beat_class ?? 'N', queued = isQueued(t.id);
    const fl = (kind, v) => (v ? `<span class="fl" data-fa="${kind}" title="クリックで「${esc(v)}」に絞り込む">${esc(v)}</span>` : '');
    return `<tr data-id="${t.id}"><td><span class="load la" data-load="A">A</span><span class="load lb" data-load="B">B</span>` +
      `<span class="load lq ${queued ? 'added' : ''}" data-q>${queued ? '✓キュー' : '＋キュー'}</span>` +
      (t.analysis_status === 'done' ? `<span class="load lm" data-match title="この曲の次に合う曲（おまかせの相性）">合う曲</span>` : '') + `</td>` +
      `<td class="n">${t.pl_pos != null ? t.pl_pos + 1 : ''}</td>` +
      `<td title="${esc(t.title)}">${esc(t.title)}</td><td>${fl('artist', t.artist)}</td><td>${fl('album', t.album)}</td><td class="n">${fmt(t.duration)}</td>` +
      `<td class="n">${t.bpm ? t.bpm.toFixed(1) : '—'}</td><td class="n">${t.camelot ?? '—'}</td><td class="n">${t.lufs?.toFixed(1) ?? '—'}</td>` +
      `<td>${starsHTML(t)}</td><td class="n">${t.plays || '—'}</td>` +
      `<td class="n">${lastPlayed(t.last_played)}${t.last_itunes ? ' <span class="itb" title="iTunes で再生">iTunes</span>' : ''}</td>` +
      `<td><span class="cls c${cls}">${cls === 'N' ? '·' : cls}</span> <span class="anst ${t.analysis_status}">${AN[t.analysis_status] ?? ''}</span></td></tr>`;
  }

  async function load(reset) {
    if (loading && !reset) return;
    const my = ++seq;
    loading = true;
    if (reset) { rows = []; total = 0; box.scrollTop = 0; }
    // アーティストを選び直したら、アルバムの一覧もその人のものに
    if (reset && view.mode === 'album' && facetCache.key !== facetKey()) refreshFolders();
    try {
      const r = await api.library({ q: filter.q, folder: filter.folder ?? undefined, root: filter.root ?? undefined,
                                    analyzed: filter.analyzed ?? undefined, artist: filter.artist ?? undefined, album: filter.album ?? undefined,
                                    playlist: filter.playlist ?? undefined,
                                    sort: view.sort.key, desc: view.sort.desc || undefined, offset: rows.length, limit: PAGE });
      if (my !== seq) return;   // 途中で条件が変わった古い応答は捨てる
      total = r.total;
      rows = rows.concat(r.items);
      remember(r.items);
      if (reset) {
        tbl.style.width = `${tableWidth()}px`;
        tbl.innerHTML = header() + r.items.map(rowHTML).join('');
        renderChips();
      } else tbl.insertAdjacentHTML('beforeend', r.items.map(rowHTML).join(''));
    } finally {
      if (my === seq) loading = false;
    }
  }

  // ---- 絞り込みの表示（一覧の上）。✕ でその条件だけ外す ----
  function renderChips() {
    const c = [];
    if (filter.analyzed) c.push(['analyzed', '解析済み']);
    if (filter.folder !== null) c.push(['folder', `フォルダ: ${filter.folder || '（直下）'}`]);
    else if (filter.root !== null) c.push(['folder', `音楽フォルダ: ${filter.root}`]);
    if (filter.artist !== null) c.push(['artist', `アーティスト: ${filter.artist || UNKNOWN}`]);
    if (filter.album !== null) c.push(['album', `アルバム: ${filter.album || UNKNOWN}`]);
    if (filter.playlist !== null) c.push(['playlist', `プレイリスト: ${filter.playlistName}`]);
    if (filter.q) c.push(['q', `検索: ${filter.q}`]);
    chips.innerHTML = `<span class="cnt">${total.toLocaleString()} 曲</span>` +
      c.map(([k, l]) => `<span class="chip" data-chip="${k}">${esc(l)}<b title="この条件を外す">✕</b></span>`).join('') +
      (c.length > 1 ? '<span class="chipall" data-chip="all">すべて外す</span>' : '') +
      (filter.playlist !== null && total ? `<span style="flex:1"></span><span class="btn" data-plq title="表示している曲を、今の並びでキューの後ろに足します">この ${total} 曲をキューに入れる</span>` : '');
  }
  chips.addEventListener('click', async e => {
    if (e.target.closest('[data-plq]')) {            // プレイリストの曲をキューへ（Phase 5 E）
      const all = [];
      for (let off = 0; off < total; off += 1000) {
        const r = await api.library({ q: filter.q, playlist: filter.playlist, artist: filter.artist ?? undefined, album: filter.album ?? undefined,
                                      analyzed: filter.analyzed ?? undefined, folder: filter.folder ?? undefined, root: filter.root ?? undefined,
                                      sort: view.sort.key, desc: view.sort.desc || undefined, offset: off, limit: 1000 });
        all.push(...r.items);
      }
      for (const t of all) if (!isQueued(t.id)) onAddToQueue(t);
      tbl.querySelectorAll('[data-q]').forEach(b => { b.classList.add('added'); b.textContent = '✓キュー'; });
      return;
    }
    const k = e.target.closest('[data-chip]')?.dataset.chip;
    if (!k || (k !== 'all' && !e.target.closest('b'))) return;
    if (k === 'all' || k === 'analyzed') filter.analyzed = null;
    if (k === 'all' || k === 'folder') filter.folder = filter.root = null;
    if (k === 'all' || k === 'artist') filter.artist = null;
    if (k === 'all' || k === 'album') filter.album = null;
    if (k === 'all' || k === 'playlist') {
      filter.playlist = null;
      if (plSortBefore) { view.sort = plSortBefore; plSortBefore = null; }
    }
    if (k === 'all' || k === 'q') { filter.q = ''; search.value = ''; }
    syncTree();
    load(true);
  });

  /** 左の一覧の「選択中」を絞り込みに合わせる。 */
  function syncTree() {
    const none = !filter.analyzed && filter.folder === null && filter.root === null;
    tree.querySelectorAll('.fold').forEach(x => {
      let on;
      if (x.dataset.f === 'all') on = none && filter.artist === null && filter.album === null && filter.playlist === null;
      else if (x.dataset.f === 'analyzed') on = !!filter.analyzed;
      else if (x.dataset.fa === 'artist') on = filter.artist === x.dataset.v;
      else if (x.dataset.fa === 'album') on = filter.album === x.dataset.v;
      else if (x.dataset.pl !== undefined) on = filter.playlist === +x.dataset.pl;
      else if (x.dataset.root !== undefined) on = filter.root === x.dataset.root && (x.dataset.folder === undefined ? filter.folder === null : filter.folder === x.dataset.folder);
      x.classList.toggle('on', !!on);
    });
  }

  box.addEventListener('scroll', () => {
    if (rows.length < total && box.scrollTop + box.clientHeight > box.scrollHeight - 400) load(false);
  });

  let t;
  search.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => { filter.q = search.value.trim(); load(true); }, 200);
  });

  tree.addEventListener('click', e => {
    const m = e.target.closest('[data-mode]');
    if (m) { view.mode = m.dataset.mode; saveView(); findEl.value = ''; refreshFolders(); return; }
    const f = e.target.closest('.fold');
    if (!f) return;
    if (f.dataset.f === 'all') {                       // すべての曲 = 条件をすべて外す
      Object.assign(filter, { folder: null, root: null, analyzed: null, artist: null, album: null, playlist: null });
    } else if (f.dataset.f === 'analyzed') {
      Object.assign(filter, { folder: null, root: null, analyzed: true });
    } else if (f.dataset.fa === 'artist') {            // もう一度クリックで外す
      const v = f.dataset.v;
      filter.artist = filter.artist === v ? null : v;
      filter.album = null;
    } else if (f.dataset.fa === 'album') {
      const v = f.dataset.v;
      filter.album = filter.album === v ? null : v;
    } else if (f.dataset.pl !== undefined) {           // iTunes のプレイリスト（もう一度クリックで外す）
      if (f.classList.contains('plfolder')) return;
      const v = +f.dataset.pl;
      filter.playlist = filter.playlist === v ? null : v;
      filter.playlistName = f.dataset.name;
      // プレイリストを選んだら、並びはプレイリストの順に（外したら元の並べ方に戻す。覚えている並べ方は変えない）
      if (filter.playlist !== null) { if (view.sort.key !== 'pos') plSortBefore = view.sort; view.sort = { key: 'pos', desc: false }; }
      else if (plSortBefore) { view.sort = plSortBefore; plSortBefore = null; }
    } else {
      Object.assign(filter, { folder: f.dataset.folder ?? null, root: f.dataset.root ?? null, analyzed: null });
    }
    syncTree();
    load(true);
  });
  findEl.addEventListener('input', () => (view.mode === 'playlist' ? renderPlaylists() : renderFacetList()));

  tbl.addEventListener('click', e => {
    if (e.target.closest('.rsz')) return;
    const th = e.target.closest('th[data-sort]');
    if (th) {
      const k = th.dataset.sort;
      view.sort = view.sort.key === k ? { key: k, desc: !view.sort.desc } : { key: k, desc: FIRST_DESC.has(k) };
      saveView();
      load(true);
      return;
    }
    const tr = e.target.closest('tr[data-id]');
    if (!tr) return;
    const track = rows.find(r => r.id === +tr.dataset.id);
    const fa = e.target.closest('[data-fa]');
    if (fa) {                                          // 表のアーティスト・アルバムをクリック → 絞り込み（I-4）
      if (fa.dataset.fa === 'artist') { filter.artist = track.artist ?? ''; filter.album = null; }
      else filter.album = track.album ?? '';
      syncTree();
      if (view.mode !== 'folder') refreshFolders();
      load(true);
    } else if (e.target.closest('[data-q]')) {
      onAddToQueue(track);
      const b = e.target.closest('[data-q]');
      b.classList.add('added');
      b.textContent = '✓キュー';
    } else if (e.target.closest('[data-match]')) {
      onMatch?.(track);
    } else if (e.target.closest('[data-load]')) {
      onLoadDeck(e.target.closest('[data-load]').dataset.load, track);
    }
  });
  tbl.addEventListener('dblclick', e => {
    const r = e.target.closest('.rsz');
    if (r) {                                           // 列の幅を元に戻す
      delete view.widths[r.dataset.rsz];
      tbl.querySelector(`col[data-c="${r.dataset.rsz}"]`).style.width = `${widthOf(r.dataset.rsz)}px`;
      tbl.style.width = `${tableWidth()}px`;
      saveView();
      return;
    }
    const tr = e.target.closest('tr[data-id]');
    if (tr && !e.target.closest('.load') && !e.target.closest('[data-fa]')) onLoadDeck(null, rows.find(r => r.id === +tr.dataset.id));
  });

  // ---- 列の幅（見出しの右端をドラッグ） ----
  tbl.addEventListener('pointerdown', e => {
    const r = e.target.closest('.rsz');
    if (!r || e.button !== 0) return;
    e.preventDefault();
    const k = r.dataset.rsz, th = r.parentElement, col = tbl.querySelector(`col[data-c="${k}"]`);
    const ratio = (th.getBoundingClientRect().width / th.offsetWidth) || 1;     // 拡大表示でもマウスの移動量と合わせる
    const x0 = e.clientX, w0 = widthOf(k);
    r.setPointerCapture(e.pointerId);
    document.body.classList.add('colresizing');
    const move = ev => {
      view.widths[k] = Math.round(Math.max(MIN_W, w0 + (ev.clientX - x0) / ratio));
      col.style.width = `${view.widths[k]}px`;
      tbl.style.width = `${tableWidth()}px`;
    };
    const up = () => {
      r.removeEventListener('pointermove', move);
      r.removeEventListener('pointerup', up);
      r.removeEventListener('pointercancel', up);
      document.body.classList.remove('colresizing');
      saveView();
    };
    r.addEventListener('pointermove', move);
    r.addEventListener('pointerup', up);
    r.addEventListener('pointercancel', up);
  });

  // ---- 左の一覧（フォルダ／アーティスト／アルバム） ----
  async function refreshFolders() {
    tree.querySelectorAll('[data-mode]').forEach(x => x.classList.toggle('on', x.dataset.mode === view.mode));
    document.getElementById('facetfind').style.display = view.mode === 'folder' ? 'none' : '';
    const r = await api.folders();
    document.getElementById('cntAll').textContent = r.counts.all.toLocaleString();
    document.getElementById('cntAn').textContent = r.counts.analyzed.toLocaleString();
    if (view.mode === 'playlist') { await renderPlaylists(); return; }
    if (view.mode !== 'folder') {
      // アルバムの一覧は、アーティストを選んでいればその人のアルバムだけ
      const key = facetKey();
      const f = await api.facets({ kind: view.mode, artist: view.mode === 'album' && filter.artist !== null ? filter.artist : undefined });
      facetCache = { key, items: f.items };
      renderFacetList();
      return;
    }
    // 音楽フォルダごとにまとめる（I-3）。音楽フォルダが 1 つなら見出しは出さない
    const multi = r.roots.length > 1;
    const base = p => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;
    listEl.innerHTML = r.roots.map(root => {
      const fs = r.folders.filter(f => f.root === root);
      const n = fs.reduce((a, f) => a + f.count, 0);
      return (multi ? `<div class="fold rootf" data-root="${esc(root)}" title="${esc(root)}">` +
        `<em>📁 ${esc(base(root))}</em><span>${n.toLocaleString()}</span></div>` : '') +
        (!fs.length ? `<div class="lbl2" style="padding:2px 12px 6px ${multi ? 26 : 12}px">曲がありません（見つからないか、走査中）</div>` : '') +
        fs.map(f => `<div class="fold ${multi ? 'sub' : ''}" data-root="${esc(root)}" data-folder="${esc(f.folder)}" title="${esc(f.folder)}">` +
          `<em>${esc(f.folder || '（直下）')}</em><span>${f.count}</span></div>`).join('');
    }).join('');
    syncTree();
  }

  /** iTunes のプレイリスト（フォルダの形のまま。⚙ はスマートプレイリスト。Phase 5 E）。 */
  async function renderPlaylists() {
    const r = await fetch('/api/itunes/playlists').then(x => x.json()).catch(() => ({ items: [] }));
    const w = findEl.value.trim().toLowerCase();
    const kids = new Map();
    for (const p of r.items) { const k = p.parent ?? ''; if (!kids.has(k)) kids.set(k, []); kids.get(k).push(p); }
    const out = [];
    const walk = (parent, depth) => {
      for (const p of kids.get(parent) ?? []) {
        const pad = `style="padding-left:${12 + depth * 14}px"`;
        if (p.folder) {
          out.push(`<div class="fold plfolder" data-pl="${p.id}" ${pad}><em>📁 ${esc(p.name)}</em></div>`);
          walk(p.pid, depth + 1);
        } else if (!w || p.name.toLowerCase().includes(w)) {
          out.push(`<div class="fold" data-pl="${p.id}" data-name="${esc(p.name)}" ${pad} title="${esc(p.name)}（解析済み ${p.analyzed} 曲）">` +
            `<em>${p.smart ? '⚙ ' : ''}${esc(p.name)}</em><span>${p.count}</span></div>`);
        }
      }
    };
    walk('', 0);
    listEl.innerHTML = out.join('') || `<div class="lbl2" style="padding:8px 12px;line-height:1.8">iTunes のプレイリストがありません。<br>設定タブの「iTunes との連携」を見てください。</div>`;
    syncTree();
  }

  function renderFacetList() {
    if (view.mode === 'folder' || view.mode === 'playlist') return;
    const w = findEl.value.trim().toLowerCase();
    const items = facetCache.items.filter(x => !w || x.name.toLowerCase().includes(w) || (x.artist ?? '').toLowerCase().includes(w));
    const head = view.mode === 'album' && filter.artist !== null
      ? `<div class="lbl2" style="padding:4px 12px">「${esc(filter.artist || UNKNOWN)}」のアルバム</div>` : '';
    const MAX = 3000;                                  // 多すぎるときは先頭だけ（「一覧の中を探す」で絞る）
    listEl.innerHTML = head + items.slice(0, MAX).map(x => {
      const sub = view.mode === 'album' ? `<small>${x.artists > 1 ? '複数のアーティスト' : esc(x.artist || UNKNOWN)}</small>` : '';
      return `<div class="fold facet" data-fa="${view.mode}" data-v="${esc(x.name)}" title="${esc(x.name || UNKNOWN)}">` +
        `<em>${esc(x.name || UNKNOWN)}${sub}</em><span>${x.count}</span></div>`;
    }).join('') + (items.length > MAX ? `<div class="lbl2" style="padding:6px 12px">ほか ${(items.length - MAX).toLocaleString()} 件（上の欄で探してください）</div>` : '')
      + (!items.length ? '<div class="lbl2" style="padding:6px 12px">ありません</div>' : '');
    syncTree();
  }

  // 走査の進捗。走査中は 1 秒ごとに状態を見て、終わったら一覧を読み直す
  async function pollScan() {
    const s = await api.scanStatus().catch(() => null);
    const el = document.getElementById('libstatus');
    if (!s) return;
    if (s.error) el.textContent = `走査エラー: ${s.error}`;
    else if (s.running) el.textContent = `走査中 ${s.done.toLocaleString()} / ${s.found ? s.found.toLocaleString() : '…'} 曲`;
    else el.textContent = `${(s.roots ?? [s.root]).length > 1 ? `音楽フォルダ ${s.roots.length} か所` : s.root}（${s.found.toLocaleString()} 曲）` +
      (s.offline?.length ? `／見つからないフォルダ: ${s.offline.join('、')}` : '');
    if (s.running) { setTimeout(pollScan, 1000); scanning = true; }
    else if (scanning) { scanning = false; refreshFolders(); load(true); }
  }
  let scanning = false;

  (async () => {
    await loadView();
    refreshFolders();
    load(true);
    pollScan();
  })();

  /** 解析状態などが変わった曲の行だけ描き直す。 */
  function updateRow(t) {
    const i = rows.findIndex(r => r.id === t.id);
    if (i < 0) return;
    rows[i] = { ...rows[i], ...t };
    const tr = tbl.querySelector(`tr[data-id="${t.id}"]`);
    if (tr) tr.outerHTML = rowHTML(rows[i]);
  }

  /** 音楽フォルダを変えた後: 絞り込みを「すべての曲」に戻し、走査が終わるのを見る。 */
  function rootsChanged() {
    Object.assign(filter, { folder: null, root: null, analyzed: null, artist: null, album: null, playlist: null });
    scanning = true;
    refreshFolders(); load(true); pollScan();
  }

  return { reload: () => load(true), refreshFolders, updateRow, rootsChanged, rescan: async () => { await api.rescan(); pollScan(); } };
}
