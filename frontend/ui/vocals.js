// パートの量（音源分離。Phase 4 B1。design_phase4.md §3）の読み込みと、ボーカルが重ならない重なりの探索。
// 探索は djmix/planner/engine.py の best_vocal_fit と同じ規則（おまかせ・つなぎ目エディタで同じ答えになるように）。
export const VOCAL_ON = 0.25;
export const STEM_COLORS = { vocals: '#f472b6', drums: '#fbbf24', bass: '#60a5fa', other: '#a3a3a3' };
export const STEM_NAMES = { vocals: 'ボーカル', drums: 'ドラム', bass: 'ベース', other: 'その他' };

const store = new Map();          // track_id → Promise<{sources, bars}>|結果

/** パートの量。まだ分離していなければサーバーが裏で始めるので、終わるまで少しずつ聞き直す（最大 60 秒）。onUpdate は届いたときに呼ぶ。 */
export function stemsOf(id, onUpdate) {
  const got = store.get(id);
  if (got && !(got instanceof Promise)) return got;
  if (!got) {
    const p = (async () => {
      for (let k = 0; k < 30; k++) {
        const r = await fetch(`/api/tracks/${id}/stems`).then(x => (x.ok ? x.json() : null)).catch(() => null);
        if (!r) break;
        if (r.status === 'done') { store.set(id, r); onUpdate?.(r); return r; }
        await new Promise(res => setTimeout(res, 2000));
      }
      store.delete(id);
      return null;
    })();
    store.set(id, p);
  }
  return null;
}

/** 使う範囲（小節 b0〜b1）のボーカルの量の列。 */
export const vocalsOf = (st, b0, b1) => (st ? st.bars.slice(b0, b1).map(r => r[0]) : null);

/** ボーカルが重ならない重なりの長さと位置（前の曲の終わりを最大 maxTrim 小節早める。重なりは 16/12/8 から）。 */
export function bestVocalFit(vo, vi, bars, maxTrim = 16, minLeft = 8) {
  const clash = (trim, n) => {
    const end = vo.length - trim;
    let c = 0;
    for (let k = 0; k < n; k++) { const a = vo[end - n + k], b = vi[k]; if (a !== undefined && b !== undefined && a > VOCAL_ON && b > VOCAL_ON) c++; }
    return c;
  };
  const c0 = clash(0, bars);
  let best = { bars, trim: 0, clash: c0, clash0: c0, cost: c0 * 10 };
  if (!c0) return best;
  for (const n of [...new Set([bars, 12, 8].filter(x => x <= bars))].sort((a, b) => b - a)) {
    for (let trim = 0; trim <= maxTrim; trim++) {
      if (vo.length - trim < n + minLeft) break;
      const c = clash(trim, n), cost = c * 10 + trim * 0.2 + (bars - n) * 0.3;
      if (cost < best.cost) best = { bars: n, trim, clash: c, clash0: c0, cost };
    }
  }
  return best;
}
