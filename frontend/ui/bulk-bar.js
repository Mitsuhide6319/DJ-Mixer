// ライブラリのまとめて解析の帯（design_phase3.md §3.3。モックアップ mockup/phase3 の共通部品）。
// 自動では始めない。「まとめて解析」「範囲を選んで先に解析…」を押したときだけ動く。
import { openModal, closeModal } from './modal.js';

const get = url => fetch(url).then(r => r.json());
const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) }).then(r => r.json());
const hm = sec => sec >= 3600 ? `約${(sec / 3600).toFixed(1)}時間` : `約${Math.max(1, Math.round(sec / 60))}分`;
const PER_TRACK = 0.77;         // 見積もり（4 並列で 1 曲あたり約 0.77 秒。2026-10-01 の GPU の順番待ちを入れた後の計測）
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function initBulkBar(el, { flash, onDone } = {}) {
  let st = null, timer = null, lastDone = 0;

  async function refresh() {
    try { st = await get('/api/bulk'); } catch { return; }
    render();
    if (st.done !== lastDone) { lastDone = st.done; onDone?.(st); }
    clearTimeout(timer);
    timer = setTimeout(refresh, st.running ? 2000 : 15000);
  }

  function render() {
    const pct = st.total ? (st.analyzed / st.total) * 100 : 0;
    const scope = st.groups?.length && !st.rest ? `（${st.groups.join('・')}だけ）` : st.groups?.length ? `（${st.groups.join('・')}を先に）` : '';
    const msg = st.running
      ? `解析中${scope}… ${st.eta_sec ? `残り ${hm(st.eta_sec)}` : '速さを測っています'}${st.playing ? '（再生中なので 1 曲ずつ）' : ''}` +
        (st.restarts ? `（止まった解析を ${st.restarts} 回立て直しました）` : '')
      : `おまかせは解析済みの曲から選びます。${st.left_all ? `残り ${st.left_all.toLocaleString()} 曲を全部で${hm(st.left_all * PER_TRACK)}` : 'すべて解析済みです'}`;
    el.innerHTML = `<div class="anbar"><b>ライブラリの解析</b>
      <div class="meter"><i style="width:${pct.toFixed(1)}%"></i></div>
      <span class="num">${st.analyzed.toLocaleString()} / ${st.total.toLocaleString()} 曲</span>
      <span class="lbl2">${msg}</span>
      ${st.failed_total ? `<span class="lbl2 flink" data-failed title="一覧を見る・やり直す">解析できなかった曲 ${st.failed_total}</span>` : ''}
      ${st.error ? `<span class="err">止まりました: ${st.error}</span>` : ''}
      <div class="sp"></div>
      <div class="btn" data-go>${st.running ? '一時停止' : st.analyzed > 0 && st.left > 0 ? 'まとめて解析（続きから）' : 'まとめて解析'}</div>
      <div class="btn" data-range>範囲を選んで先に解析…</div></div>`;
    el.querySelector('[data-go]').addEventListener('click', async () => {
      if (st.running) { st = await post('/api/bulk/pause'); flash?.('解析中の曲が終わったら止まります'); }
      else st = await post('/api/bulk/start', { groups: [], rest: true });
      render();
      refresh();
    });
    el.querySelector('[data-range]').addEventListener('click', openRange);
    el.querySelector('[data-failed]')?.addEventListener('click', openFailed);
  }

  /** 解析できなかった曲の一覧と「やり直す」。 */
  async function openFailed() {
    const { items } = await get('/api/bulk/failed');
    openModal(`<h3>解析できなかった曲（${items.length} 曲）</h3>
      <p class="lbl2" style="line-height:1.8">読めないファイル・壊れたファイルのほか、解析の途中で止まった曲もここに入ります。「やり直す」で未解析に戻して、もう一度解析します。</p>
      <div class="flist">${items.map(t => `<div><b>${esc(t.title || t.path.split(/[\\/]/).pop())}</b> <span>${esc(t.artist ?? '')}${t.album ? ` ／ ${esc(t.album)}` : ''}</span>` +
        `<small>${esc(t.error ?? '')}</small></div>`).join('') || '<p class="lbl2">ありません</p>'}</div>
      <div class="mfoot"><div class="btn" data-close>閉じる</div><div class="btn primary" data-retry>やり直す</div></div>`, m => {
      m.querySelector('[data-retry]').addEventListener('click', async () => {
        const r = await post('/api/bulk/retry-failed');
        closeModal();
        flash?.(`${r.reset} 曲を未解析に戻しました${r.running ? '。続けて解析します' : ''}`);
        refresh();
      });
    });
  }

  async function openRange() {
    const { groups } = await get('/api/genre-groups');
    openModal(`<h3>範囲を選んで先に解析</h3>
      <p class="lbl2" style="line-height:1.8">選んだジャンルのまとまりの曲を先に解析します。数字は 解析済み / 全体 の曲数です。</p>
      <div class="gchips">${groups.map(g => `<label class="gchip"><input type="checkbox" value="${g.name}">${g.name}<small>${g.analyzed} / ${g.total.toLocaleString()}</small></label>`).join('')}</div>
      <label class="lbl2"><input type="checkbox" data-rest checked> 終わったら、残りの曲も続けて解析する</label>
      <label class="lbl2"><input type="checkbox" data-stems> パート（ボーカル・ドラム・ベース・その他）の分析も行う（歌の重なりを避ける繋ぎに使う。時間は約 3〜4 倍）</label>
      <div class="lbl2" data-est></div>
      <div class="mfoot"><div class="btn" data-close>キャンセル</div><div class="btn primary" data-ok>この範囲を解析する</div></div>`, m => {
      const sel = () => [...m.querySelectorAll('.gchip input')].filter(x => x.checked).map(x => x.value);
      const est = () => {
        const n = groups.filter(g => sel().includes(g.name)).reduce((s, g) => s + g.left, 0);
        const k = m.querySelector('[data-stems]').checked ? 3.6 : 1;
        m.querySelector('[data-est]').textContent = sel().length ? `未解析 ${n.toLocaleString()} 曲 ・ ${hm(n * PER_TRACK * k)}` : 'まとまりを選んでください';
      };
      m.querySelectorAll('.gchip input').forEach(x => x.addEventListener('change', () => { x.parentElement.classList.toggle('on', x.checked); est(); }));
      m.querySelector('[data-stems]').addEventListener('change', est);
      m.querySelector('[data-ok]').addEventListener('click', async () => {
        if (!sel().length) return;
        st = await post('/api/bulk/start', { groups: sel(), rest: m.querySelector('[data-rest]').checked, stems: m.querySelector('[data-stems]').checked });
        closeModal();
        render();
        refresh();
      });
      est();
    });
  }

  refresh();
  return { refresh, get state() { return st; } };
}

/** どちらかのデッキが再生中かをサーバーに知らせる（再生中はまとめて解析を 1 曲ずつに減らす）。 */
let lastPlaying = null;
export function reportPlaying(playing) {
  if (playing === lastPlaying) return;
  lastPlaying = playing;
  post('/api/bulk/playing', { playing }).catch(() => {});
}
