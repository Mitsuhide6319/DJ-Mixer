// Phase 3 モックアップ共通（案X・Y・Z）。データは試作（work/phase3/planner_proto.py）の実際の結果。
(function () {
  const D = window.MOCK3;
  const T = D.tracks;                       // id -> 曲
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const C = {};
  for (const n of ['--deck-a', '--deck-b', '--line', '--dim', '--faint', '--active', '--warn', '--ok', '--text', '--panel'])
    C[n] = css(n);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = s => { s = Math.max(0, s); return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`; };
  const TPL = { long: 'ロングブレンド', short: 'ショート', echo: 'エコーアウト', bass: '低音入れ替え', filter: 'フィルター', cut: 'カット' };
  const TPL_BARS = { long: 16, short: 8, echo: 2, bass: 16, filter: 16, cut: 0 };
  const FLOWS = {
    up: { name: '上げていく', f: (x, e0) => e0 + (Math.min(1, e0 + 0.45) - e0) * x },
    keep: { name: 'キープ', f: (x, e0) => e0 },
    peak: { name: '山を作る', f: (x, e0) => e0 + 0.4 * Math.sin(Math.PI * x) },
    down: { name: '落ち着かせる', f: (x, e0) => e0 - (e0 - Math.max(0, e0 - 0.45)) * x },
  };

  function fitCanvas(cv) {
    const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * d)), h = Math.max(1, Math.round(r.height * d));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const g = cv.getContext('2d');
    g.setTransform(1, 0, 0, 1, 0, 0);
    return [g, w, h, d];
  }

  /** 並び（曲の配列 [{id, join}]）を時間軸に置く。重なりは繋ぎ方の標準の小節数。 */
  function layout(rows) {
    const L = [];
    rows.forEach((r, i) => {
      const t = T[r.id], len = t.use.t1 - t.use.t0;
      let start = 0;
      if (i > 0) {
        const p = rows[i - 1], pt = T[p.id], bars = TPL_BARS[p.join?.tpl ?? 'long'];
        const ov = Math.min(bars * 4 * 60 / pt.bpm, len * 0.4, L[i - 1].len * 0.4);
        start = L[i - 1].start + L[i - 1].len - ov;
        L[i - 1].ovOut = ov;
      }
      L.push({ r, t, start, len, ovOut: 0 });
    });
    return L;
  }

  /**
   * ミックス全体のタイムライン（Phase 2 案C を土台に）。上: 曲の帯（上下交互、小節ごとの音量の輪郭）と重なり。
   * 下: エネルギーの流れ（狙いの線と、曲ごとの実際）。返り値: クリック判定用の帯の位置。
   */
  function drawTimeline(cv, rows, { flow = null, e0 = 0.5, sel = -1, hover = -1, minutes = null, target = null } = {}) {
    const [g, W, H, d] = fitCanvas(cv);
    const L = layout(rows);
    const total = Math.max(minutes ? minutes * 60 : 0, L.length ? L[L.length - 1].start + L[L.length - 1].len : 60);
    const pad = 10 * d, X = s => pad + s / total * (W - 2 * pad);
    const laneH = 46 * d, top = 22 * d, eTop = top + 2 * laneH + 26 * d, eH = H - eTop - 18 * d;
    g.fillStyle = '#050607'; g.fillRect(0, 0, W, H);
    // 目盛り（5 分ごと）
    g.font = `${10 * d}px Consolas`;
    for (let m = 0; m * 60 <= total; m += 5) {
      const x = Math.round(X(m * 60));
      g.fillStyle = 'rgba(255,255,255,.08)'; g.fillRect(x, top, d, H - top - 14 * d);
      g.fillStyle = C['--faint']; g.fillText(`${m}分`, x + 3 * d, 14 * d);
    }
    const boxes = [];
    L.forEach((l, i) => {
      const lane = i % 2, y = top + lane * laneH, x0 = X(l.start), x1 = X(l.start + l.len);
      const col = lane ? C['--deck-b'] : C['--deck-a'];
      g.fillStyle = i === sel ? 'rgba(255,255,255,.14)' : i === hover ? 'rgba(255,255,255,.09)' : 'rgba(255,255,255,.05)';
      g.fillRect(x0, y + 2 * d, x1 - x0, laneH - 4 * d);
      // 小節ごとの音量の輪郭（使う範囲の中だけ）
      const t = l.t, b0 = t.use.bar0, b1 = t.use.bar1, mid = y + laneH / 2, amp = (laneH / 2 - 5 * d);
      g.beginPath(); g.moveTo(x0, mid);
      const pts = [];
      for (let b = b0; b < b1; b++) {
        const tt = (t.bars[b] ?? t.duration) - t.use.t0;
        pts.push([X(l.start + tt), (t.rms[b] ?? 0) * 0.85 + 0.08]);
      }
      pts.forEach(([x, v]) => g.lineTo(x, mid - v * amp));
      for (let k = pts.length - 1; k >= 0; k--) g.lineTo(pts[k][0], mid + pts[k][1] * amp);
      g.closePath(); g.globalAlpha = 0.75; g.fillStyle = col; g.fill(); g.globalAlpha = 1;
      if (i === sel) { g.strokeStyle = '#fff'; g.lineWidth = 2 * d; g.strokeRect(x0, y + 2 * d, x1 - x0, laneH - 4 * d); }
      // 曲名
      g.save(); g.beginPath(); g.rect(x0, y, x1 - x0, laneH); g.clip();
      g.fillStyle = '#fff'; g.font = `600 ${11 * d}px "Yu Gothic UI"`;
      g.shadowColor = '#000'; g.shadowBlur = 3 * d;
      g.fillText(`${i + 1}. ${t.title}`, x0 + 5 * d, y + 14 * d);
      g.restore();
      boxes.push({ i, x0: x0 / d, x1: x1 / d, y0: y / d, y1: (y + laneH) / d });
    });
    // 重なり（繋ぎ）
    L.forEach((l, i) => {
      if (!l.ovOut) return;
      const x0 = X(l.start + l.len - l.ovOut), x1 = X(l.start + l.len);
      const tpl = l.r.join?.tpl;
      g.fillStyle = tpl === 'echo' ? 'rgba(234,179,8,.22)' : 'rgba(34,197,94,.22)';
      g.fillRect(x0, top, x1 - x0, 2 * laneH);
      g.fillStyle = tpl === 'echo' ? C['--warn'] : C['--ok'];
      g.font = `${10 * d}px "Yu Gothic UI"`;
      g.fillText(TPL[tpl] ?? '', x0, top + 2 * laneH + 13 * d);
    });
    // エネルギーの流れ
    g.fillStyle = 'rgba(255,255,255,.03)'; g.fillRect(pad, eTop, W - 2 * pad, eH);
    g.fillStyle = C['--dim']; g.font = `${10 * d}px "Yu Gothic UI"`; g.fillText('エネルギー（上ほど盛り上がる）', pad + 4 * d, eTop + 12 * d);
    const Y = e => eTop + eH - Math.max(0, Math.min(1, e)) * (eH - 16 * d) - 4 * d;
    if (target) {                    // 手描きの線（案Z）
      g.strokeStyle = 'rgba(255,255,255,.55)'; g.lineWidth = 2 * d; g.setLineDash([6 * d, 5 * d]); g.beginPath();
      target.forEach((e, k) => { const x = X(k / (target.length - 1) * total); k ? g.lineTo(x, Y(e)) : g.moveTo(x, Y(e)); });
      g.stroke(); g.setLineDash([]);
    } else if (flow) {
      g.strokeStyle = 'rgba(255,255,255,.55)'; g.lineWidth = 2 * d; g.setLineDash([6 * d, 5 * d]); g.beginPath();
      for (let k = 0; k <= 60; k++) { const x = X(k / 60 * total), y = Y(FLOWS[flow].f(k / 60, e0)); k ? g.lineTo(x, y) : g.moveTo(x, y); }
      g.stroke(); g.setLineDash([]);
    }
    g.strokeStyle = '#fb923c'; g.lineWidth = 2.5 * d; g.beginPath();
    L.forEach((l, i) => { const x = X(l.start + l.len / 2), y = Y(l.t.energy); i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
    L.forEach(l => { g.fillStyle = '#fb923c'; g.beginPath(); g.arc(X(l.start + l.len / 2), Y(l.t.energy), 4 * d, 0, 7); g.fill(); });
    return { boxes, total, L, eTop: eTop / d, eH: eH / d, pad: pad / d, W: W / d };
  }

  /** 相性の内訳（テンポ・キー・響き・雰囲気）の小さな棒。 */
  function partsHTML(p) {
    const items = [['テンポ', p.tempo], ['キー', p.key], ['響き', p.chroma], ['雰囲気', p.mood]];
    return `<div class="parts">${items.map(([n, v]) => `<span class="pt"><i>${n}</i><b style="width:${Math.round(v * 44)}px;background:${v > 0.75 ? C['--ok'] : v > 0.45 ? C['--warn'] : C['--active']}"></b></span>`).join('')}</div>`;
  }
  const scoreColor = s => s > 0.8 ? C['--ok'] : s > 0.65 ? C['--warn'] : C['--active'];

  /** ミックスキュー（右側）。 */
  function renderQueue(el, rows, note = '') {
    let h = '', total = 0;
    const L = layout(rows);
    rows.forEach((r, i) => {
      const t = T[r.id];
      total = L[i].start + L[i].len;
      h += `<div class="qi"><div class="bar"></div><div class="no">${i + 1}</div><div><div class="t1">${esc(t.title)}</div>
        <div class="t2">${esc(t.artist ?? '')} ・ ${fmt(t.use.t0)}–${fmt(t.use.t1)}</div></div>
        <div class="meta">${t.bpm.toFixed(1)} <span>BPM</span><br>${t.camelot}</div></div>`;
      if (r.join) h += `<div class="qtr"><span class="arr">↓</span>${TPL[r.join.tpl]} ${TPL_BARS[r.join.tpl]}小節<span class="how" style="color:${scoreColor(r.join.score)}">相性 ${r.join.score.toFixed(2)}</span></div>`;
    });
    el.innerHTML = `<div class="qhead"><h3>ミックスキュー <small>${rows.length}曲 ・ 約${Math.round(total / 60)}分</small></h3></div>
      <div class="qlist">${h || '<div class="jempty">空です</div>'}</div>
      <div class="qfoot">${note ? `<div class="lbl2" style="color:var(--ok)">${note}</div>` : ''}
        <div class="qmix"><div class="btn" id="qmix">▶ ミックスを通して再生</div><div class="btn">■ 止める</div></div>
        <div class="btn" data-export>⤓ ミックスを書き出す…</div></div>`;
    el.querySelector('[data-export]').addEventListener('click', openExport);
  }

  // ---- 共通部品: ライブラリの解析の進み具合 ----
  function analysisBar(el) {
    const lib = D.library;
    let running = false, done = lib.analyzed, timer = null;
    const draw = () => {
      const pct = done / lib.tracks * 100, left = (lib.tracks - done) * 1.05 / 3600;
      el.innerHTML = `<div class="anbar"><b>ライブラリの解析</b>
        <div class="meter"><i style="width:${pct.toFixed(1)}%"></i></div>
        <span class="num">${done.toLocaleString()} / ${lib.tracks.toLocaleString()} 曲</span>
        <span class="lbl2">${running ? `解析中… 残り 約${left.toFixed(1)}時間（再生中は控えめに進めます）` : `おまかせは解析済みの曲から選びます。全曲で約${(lib.tracks * 1.05 / 3600).toFixed(0)}時間`}</span>
        <div class="sp"></div>
        <div class="btn" data-an>${running ? '一時停止' : done > lib.analyzed ? '続きから解析' : 'まとめて解析'}</div>
        <div class="btn" data-an-range>範囲を選んで先に解析…</div></div>`;
      el.querySelector('[data-an]').addEventListener('click', () => {
        running = !running;
        clearInterval(timer);
        if (running) timer = setInterval(() => { done = Math.min(lib.tracks, done + 37); draw(); }, 400);
        draw();
      });
      el.querySelector('[data-an-range]').addEventListener('click', () => openRange());
    };
    draw();
  }
  function openRange() {
    modal(`<h3>範囲を選んで先に解析</h3>
      <p class="lbl2" style="line-height:1.8">選んだ範囲の曲を優先して解析します。終わったら、残りの曲の解析を続けるか選べます。</p>
      <div class="gchips">${D.groups.map((g, i) => `<label class="gchip ${i === 0 ? 'on' : ''}"><input type="checkbox" ${i === 0 ? 'checked' : ''}>${g.name}<small>${g.analyzed} / ${g.total.toLocaleString()} 曲</small></label>`).join('')}</div>
      <div class="lbl2" style="margin-top:10px" data-est></div>
      <div class="mfoot"><div class="btn" data-close>キャンセル</div><div class="btn primary" data-close>この範囲を解析する</div></div>`, m => {
      const est = () => {
        const n = [...m.querySelectorAll('.gchip')].reduce((s, el, i) => s + (el.querySelector('input').checked ? D.groups[i].total - D.groups[i].analyzed : 0), 0);
        m.querySelector('[data-est]').textContent = `未解析 ${n.toLocaleString()} 曲 ・ 約${Math.max(1, Math.round(n * 1.05 / 60))}分`;
      };
      m.querySelectorAll('.gchip input').forEach(x => x.addEventListener('change', () => { x.parentElement.classList.toggle('on', x.checked); est(); }));
      est();
    });
  }

  // ---- 共通部品: 書き出し（2 方式） ----
  function openExport() {
    modal(`<h3>ミックスを書き出す</h3>
      <div class="exopts">
        <label class="exopt on"><input type="radio" name="ex" checked><b>高速で書き出す</b><span>再生せずに計算して書き出します。40 分のミックスで約 1〜2 分。<br>繋ぎは自動の動き（つなぎ目エディタの線）どおり。再生中に手で動かした操作は入りません。</span></label>
        <label class="exopt"><input type="radio" name="ex"><b>再生しながら録音する</b><span>「ミックスを通して再生」の音をそのまま録音します。ミックスの長さだけ時間がかかります（40 分なら 40 分）。<br>再生中に手で動かしたフェーダーやつまみも、そのまま入ります。</span></label>
      </div>
      <div class="exrow"><span class="lbl2">形式</span><div class="seg2"><div class="on">WAV（劣化なし）</div><div>MP3 320kbps</div><div>MP3 192kbps</div></div></div>
      <div class="exrow"><span class="lbl2">保存先</span><input value="G:\\MIX\\2026-09-30 おまかせ（上げていく）.wav" style="flex:1"></div>
      <div class="exrow"><span class="lbl2">曲の区切り</span><label><input type="checkbox" checked> 曲目リスト（開始時刻つき）も書き出す</label></div>
      <p class="lbl2" style="line-height:1.7">書き出したミックスは個人で楽しむ範囲でお使いください。</p>
      <div class="mfoot"><div class="btn" data-close>キャンセル</div><div class="btn primary" data-go>書き出す</div></div>`, m => {
      m.querySelectorAll('.exopt input').forEach(x => x.addEventListener('change', () => m.querySelectorAll('.exopt').forEach(o => o.classList.toggle('on', o.querySelector('input').checked))));
      m.querySelectorAll('.seg2 div').forEach(x => x.addEventListener('click', () => m.querySelectorAll('.seg2 div').forEach(o => o.classList.toggle('on', o === x))));
      m.querySelector('[data-go]').addEventListener('click', () => {
        const fast = m.querySelector('.exopt input').checked;
        const f = m.querySelector('.mfoot');
        let p = 0;
        f.innerHTML = `<div class="meter" style="flex:1"><i style="width:0%"></i></div><span class="lbl2" data-p></span>`;
        const iv = setInterval(() => {
          p = Math.min(100, p + (fast ? 4 : 0.6));
          f.querySelector('.meter i').style.width = p + '%';
          f.querySelector('[data-p]').textContent = fast ? `書き出し中 ${Math.round(p)}%（残り 約${Math.round((100 - p) * 0.9)}秒）` : `録音中 ${fmt(p / 100 * 2400)} / 40:00（再生しています）`;
          if (p >= 100) { clearInterval(iv); f.innerHTML = `<span style="color:var(--ok)">書き出しました（モックアップのため実際のファイルはできません）</span><div class="sp"></div><div class="btn" data-close>閉じる</div>`; f.querySelector('[data-close]').addEventListener('click', close); }
        }, 120);
      });
    });
  }

  let modalEl = null;
  function close() { modalEl?.remove(); modalEl = null; }
  function modal(html, bind) {
    close();
    modalEl = document.createElement('div');
    modalEl.className = 'mback';
    modalEl.innerHTML = `<div class="mbox">${html}</div>`;
    document.body.appendChild(modalEl);
    modalEl.addEventListener('click', e => { if (e.target === modalEl || e.target.closest('[data-close]')) close(); });
    bind?.(modalEl);
  }

  function applyScale() {
    const u = Math.max(0.85, Math.min(1.5, innerWidth / 1920, innerHeight / 1080));
    document.documentElement.style.setProperty('--u', u.toFixed(3));
  }
  addEventListener('resize', applyScale); applyScale();

  window.P3 = { D, T, C, esc, fmt, TPL, TPL_BARS, FLOWS, fitCanvas, layout, drawTimeline, partsHTML, scoreColor, renderQueue, analysisBar, openExport, modal, close };
})();
