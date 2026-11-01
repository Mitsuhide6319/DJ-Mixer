// ミックスのタブ（案D1。design_phase4.md §1.3）。作ったミックスの一覧・曲目（★）・操作・書き出したファイルと、好みの設定。
import { reveal } from './export-dialog.js';
import { openModal, closeModal } from './modal.js';
import { starsHTML, remember } from './stars.js';

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const day = ts => { if (!ts) return '—'; const d = new Date(ts * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
const fmtSec = s => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
const j = (url, method = 'GET', body) => fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
  .then(async r => { const x = await r.json(); if (!r.ok) throw new Error(x.detail ?? r.status); return x; });

/** o: {root, queue, flash, settings(), applySettings(patch), playMix(), exportMix(), afterOpen()} */
export function initMixesView(o) {
  let sort = 'new', sel = null, list = [], detail = null;
  o.root.innerHTML = `<div class="mgrid">
    <div class="mcol">
      <div class="p2card"><div class="p2title"><b>作ったミックス</b><span data-r="cnt"></span><div class="sp"></div>
        <div class="seg2" data-r="sort"><div data-s="new">新しい順</div><div data-s="name">名前順</div><div data-s="plays">よく聴く順</div></div></div>
        <div class="mixlist" data-r="list"></div>
        <p class="note">キューの「保存」、おまかせの「この案をキューに入れる」、書き出しのたびに、ここへ自動で残ります。</p></div>
      <div class="p2card"><div class="p2title"><b>好み</b><span>おまかせの選曲に使います</span></div><div data-r="prefs" class="prefs"></div></div>
      <div class="p2card"><div class="p2title"><b>最近かけた曲</b></div><div data-r="hist" class="histlist"></div></div>
    </div>
    <div class="p2card" data-r="detail"><div class="jempty">左の一覧からミックスを選んでください。</div></div>
  </div>`;
  const $ = k => o.root.querySelector(`[data-r=${k}]`);

  async function refresh() {
    const r = await j(`/api/mixes?sort=${sort}`);
    list = r.mixes;
    $('cnt').textContent = `${list.length} 件`;
    o.root.querySelectorAll('[data-s]').forEach(x => x.classList.toggle('on', x.dataset.s === sort));
    $('list').innerHTML = list.map(m => `<div class="mixrow ${m.id === sel ? 'sel' : ''}" data-id="${m.id}">
      <div><b>${esc(m.name)}</b>${m.id === r.current ? ' <span class="tag">キューで開いている</span>' : ''}<br><small>${esc(m.titles.join('・'))}${m.count > 4 ? ' …' : ''}</small></div>
      <span class="tag ${m.kind === 'auto' ? 'auto' : ''}">${m.kind === 'auto' ? 'おまかせ' : '手で作った'}</span>
      <span class="hist">${m.count}曲<br>${Math.round(m.minutes)}分</span><span class="hist">再生 ${m.plays}回</span>
      <span class="hist">${day(m.created)}${m.exports.length ? '<br>⤓ 書き出し済み' : ''}</span></div>`).join('') || '<div class="jempty">まだありません。キューの「保存」で残せます。</div>';
    if (sel != null && !list.some(m => m.id === sel)) sel = null;
    renderPrefs();
    renderHistory();
    if (sel != null) showDetail(sel); else $('detail').innerHTML = '<div class="jempty">左の一覧からミックスを選んでください。</div>';
  }

  function renderPrefs() {
    const s = o.settings();
    $('prefs').innerHTML = `<label class="opt"><input type="checkbox" data-p="prefer_rating" ${s.prefer_rating !== false ? 'checked' : ''}> ★ の多い曲を優先する（★5 は相性 +0.05、★2 は −0.025）</label>
      <label class="opt"><input type="checkbox" data-p="skip_rating1" ${s.skip_rating1 !== false ? 'checked' : ''}> ★1 の曲は使わない</label>
      <label class="opt"><input type="checkbox" data-p="auto_parts" ${s.auto_parts !== false ? 'checked' : ''}> パートごとに鳴らす繋ぎを使う（音源分離。ロングは歌を先に抜いてドラムから入れ替え、歌が重なるショートは歌を先に抜く。使う曲は 1 曲 20 秒ほどで分離して保存）</label>
      <label class="opt"><input type="checkbox" data-p="prefer_plays" ${s.prefer_plays !== false ? 'checked' : ''}> よく聴く曲を少し優先する（再生回数。いちばん聴く曲で相性 +0.03。iTunes の再生回数も数える）</label>
      <label class="opt">最近 <select data-p="avoid_recent_days">${[0, 7, 14, 30].map(d => `<option value="${d}" ${+(s.avoid_recent_days ?? 14) === d ? 'selected' : ''}>${d || '—'}</option>`).join('')}</select> 日にかけた曲は避ける（— は避けない。iTunes で再生した曲も）</label>
      <p class="note">★ はライブラリの一覧・ミックスキュー・おまかせの結果・この画面の曲目で付けられます。「かけた」は、デッキで 30 秒以上鳴った曲・ミックスを通して再生した曲・書き出した曲です。</p>`;
    $('prefs').querySelectorAll('[data-p]').forEach(x => x.addEventListener('change', () =>
      o.applySettings({ [x.dataset.p]: x.type === 'checkbox' ? x.checked : +x.value })));
  }

  async function renderHistory() {
    const r = await j('/api/plays?limit=12');
    const SRC = { deck: 'デッキ', mix: '通し再生', export: '書き出し' };
    $('hist').innerHTML = r.items.map(h => `<div class="hrow"><span class="hist">${day(h.at)}</span><span>${esc(h.title)} <small>${esc(h.artist ?? '')}</small></span><span class="hist">${SRC[h.source] ?? ''}</span></div>`).join('')
      || '<div class="note">まだありません</div>';
  }

  async function showDetail(id) {
    sel = id;
    o.root.querySelectorAll('.mixrow').forEach(x => x.classList.toggle('sel', +x.dataset.id === id));
    detail = await j(`/api/mixes/${id}`);
    remember(detail.items.map(it => it.track));
    const m = detail;
    $('detail').innerHTML = `<div class="p2title"><b>${esc(m.name)}</b><span>${m.count}曲 ・ 約${Math.round(m.minutes)}分 ・ ${day(m.created)} ・ 再生 ${m.plays}回</span></div>
      <div class="acts" style="margin:10px 0"><div class="btn primary" data-a="open">キューに開く</div><div class="btn" data-a="play">▶ 通し再生</div>
        <div class="btn" data-a="export">⤓ 書き出す…</div><div class="btn" data-a="dup">複製</div><div class="btn" data-a="rename">名前を変える</div><div class="btn" data-a="del">削除</div></div>
      <div class="lbl2" style="margin-bottom:4px">曲目（★ をクリックで評価）</div>
      ${m.items.map((it, i) => `<div class="trk"><span class="n">${i + 1}</span><div>${esc(it.track.title)} <small class="dim">${esc(it.track.artist ?? '')}</small></div>
        <span class="hist">${it.track.bpm ? it.track.bpm.toFixed(1) : '—'} ・ ${it.track.camelot ?? '—'}</span>${starsHTML(it.track)}</div>`).join('')}
      <div class="lbl2" style="margin:10px 0 4px">書き出したファイル</div>
      <div class="files">${m.exports.map((e, k) => `<div>${esc(e.path)}（${e.seconds ? fmtSec(e.seconds) : ''}）${e.tracklist ? '＋曲目リスト' : ''} <span class="btn" data-reveal="${k}" style="height:22px;font-size:11px;padding:0 8px;min-width:0" title="このファイルを選んだ状態でフォルダを開きます">📂 フォルダを開く</span></div>`).join('') || '<div>まだありません</div>'}</div>`;
    $('detail').querySelectorAll('[data-reveal]').forEach(b => b.addEventListener('click', () => reveal(m.exports[+b.dataset.reveal].path).catch(e => o.flash(e.message))));
    $('detail').querySelectorAll('[data-a]').forEach(b => b.addEventListener('click', () => act(b.dataset.a)));
  }

  async function openInQueue(then) {
    const go = async mode => {
      closeModal();
      await j(`/api/mixes/${sel}/open`, 'POST', { mode });
      await o.queue.loadQueue();
      o.flash(`「${detail.name}」をキューに${mode === 'append' ? '足しました' : '開きました'}`);
      o.afterOpen?.();
      await refresh();
      then?.();
    };
    if (!o.queue.getItems().length) { go('replace'); return; }
    openModal(`<h3>「${esc(detail.name)}」をキューに開く</h3><p class="lbl2" style="line-height:1.8">キューにはすでに ${o.queue.getItems().length} 曲あります。</p>
      <div class="mfoot"><div class="btn" data-close>やめる</div><div class="btn" data-m="append">後ろに足す</div><div class="btn primary" data-m="replace">置き換える</div></div>`,
      m => m.querySelectorAll('[data-m]').forEach(b => b.addEventListener('click', () => go(b.dataset.m))));
  }

  async function act(a) {
    if (a === 'open') return openInQueue();
    if (a === 'play') return openInQueue(() => o.playMix());
    if (a === 'export') return openInQueue(() => o.exportMix());
    if (a === 'dup') { const m = await j(`/api/mixes/${sel}/duplicate`, 'POST'); sel = m.id; o.flash(`「${m.name}」を作りました`); return refresh(); }
    if (a === 'rename') {
      return openModal(`<h3>名前を変える</h3><input data-n value="${esc(detail.name)}" class="minput">
        <div class="mfoot"><div class="btn" data-close>やめる</div><div class="btn primary" data-ok>変える</div></div>`, m => {
        const inp = m.querySelector('[data-n]'); inp.focus(); inp.select();
        const ok = async () => { await j(`/api/mixes/${sel}`, 'PATCH', { name: inp.value }); closeModal(); refresh(); };
        m.querySelector('[data-ok]').addEventListener('click', ok);
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') ok(); });
      });
    }
    if (a === 'del') {
      return openModal(`<h3>「${esc(detail.name)}」を削除しますか？</h3><p class="lbl2" style="line-height:1.8">ミックス（曲の並び・使う範囲・つなぎ）を消します。書き出したファイルと、ライブラリの曲は消えません。</p>
        <div class="mfoot"><div class="btn" data-close>やめる</div><div class="btn primary" data-ok>削除する</div></div>`, m =>
        m.querySelector('[data-ok]').addEventListener('click', async () => { await j(`/api/mixes/${sel}`, 'DELETE'); closeModal(); sel = null; o.flash('削除しました'); refresh(); }));
    }
  }

  o.root.addEventListener('click', e => {
    const s = e.target.closest('[data-s]'); if (s) { sort = s.dataset.s; refresh(); return; }
    const r = e.target.closest('.mixrow'); if (r && !e.target.closest('.stars')) showDetail(+r.dataset.id);
  });
  return { show: refresh, refresh };
}

/** 名前を付けて保存する小窓（キューの「保存」）。 */
export function askMixName({ title = 'キューをミックスとして保存', name, onOk }) {
  openModal(`<h3>${esc(title)}</h3><input data-n value="${esc(name)}" class="minput">
    <p class="lbl2">ミックスのタブの一覧に残ります。</p>
    <div class="mfoot"><div class="btn" data-close>やめる</div><div class="btn primary" data-ok>保存する</div></div>`, m => {
    const inp = m.querySelector('[data-n]'); inp.focus(); inp.select();
    const ok = () => { const v = inp.value.trim(); if (!v) return; closeModal(); onOk(v); };
    m.querySelector('[data-ok]').addEventListener('click', ok);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') ok(); });
  });
}
