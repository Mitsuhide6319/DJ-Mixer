// 汎用の操作子: ノブ(SVG)・縦/横フェーダーのドラッグ。
// 値の変化は onChange(0..1) で通知する。アクション(P1-9)への接続は呼び出し側で行う。
import { uiScale } from './scale.js';

export function knobSVG(v, col) {
  const a0 = -135, a = a0 + v * 270, r = 30, cx = 38, cy = 38;
  const pt = (deg, rr) => [cx + rr * Math.sin(deg * Math.PI / 180), cy - rr * Math.cos(deg * Math.PI / 180)];
  const [x0, y0] = pt(a0, r), [x1, y1] = pt(a, r), large = (v * 270) > 180 ? 1 : 0;
  const [ix, iy] = pt(a, 18), [ox, oy] = pt(a, 6);
  const ticks = Array.from({ length: 11 }, (_, i) => {
    const dg = a0 + i * 27, [p, q] = pt(dg, 35), [p2, q2] = pt(dg, 37.5);
    return `<line x1="${p}" y1="${q}" x2="${p2}" y2="${q2}" stroke="#4a4f58" stroke-width="1.2"/>`;
  }).join('');
  return `<svg viewBox="0 0 76 76" shape-rendering="geometricPrecision">
    <defs><radialGradient id="kg" cx="40%" cy="35%" r="70%"><stop offset="0" stop-color="#3a3f48"/><stop offset="1" stop-color="#1c1f24"/></radialGradient></defs>
    ${ticks}
    <circle cx="38" cy="38" r="30" fill="none" stroke="#262a31" stroke-width="4"/>
    ${v > 0.002 ? `<path d="M${x0} ${y0} A30 30 0 ${large} 1 ${x1} ${y1}" fill="none" stroke="${col}" stroke-width="4" stroke-linecap="round"/>` : ''}
    <circle cx="38" cy="38" r="23" fill="url(#kg)" stroke="#454a53" stroke-width="1"/>
    <line x1="${ox}" y1="${oy}" x2="${ix}" y2="${iy}" stroke="#f0f0f0" stroke-width="3" stroke-linecap="round"/></svg>`;
}

/** ノブ要素を作る。上下ドラッグで値を変え、ダブルクリックで既定値に戻す。 */
export function makeKnob({ value = 0.5, def = 0.5, color = '#9aa3ad', onChange = () => {} } = {}) {
  const el = document.createElement('div');
  el.className = 'knob';
  let v = value;
  const render = () => { el.innerHTML = knobSVG(v, color); };
  const set = (nv, notify = true) => { v = Math.max(0, Math.min(1, nv)); render(); if (notify) onChange(v); };
  el.addEventListener('pointerdown', e => {
    el.setPointerCapture(e.pointerId);
    const y0 = e.clientY, v0 = v;
    const mv = ev => set(v0 - (ev.clientY - y0) / (200 * uiScale()));
    el.addEventListener('pointermove', mv);
    el.addEventListener('pointerup', () => el.removeEventListener('pointermove', mv), { once: true });
  });
  el.addEventListener('dblclick', () => set(def));
  render();
  return { el, set, get: () => v };
}

/** フェーダー。vertical=true なら上が 1.0。キャップ位置は zoom 倍率で割り戻して計算する。 */
export function makeFader({ el, vertical = true, value = 0, onChange = () => {} }) {
  const cap = el.querySelector('.cap, .hcap');
  let v = value;
  const self = { onChange };   // onChange はあとから差し替えられる
  // キャップはレイアウト座標（zoom 前）で配置する
  const place = () => {
    if (vertical) cap.style.top = `${(1 - v) * (el.clientHeight - cap.offsetHeight)}px`;
    else cap.style.left = `${v * (el.clientWidth - cap.offsetWidth)}px`;
  };
  const set = (nv, notify = true) => { v = Math.max(0, Math.min(1, nv)); place(); if (notify) self.onChange(v); };
  el.addEventListener('pointerdown', e => {
    el.setPointerCapture(e.pointerId);
    const mv = ev => {
      const u = uiScale(), r = el.getBoundingClientRect();
      if (vertical) {
        const h = (r.height / u) - cap.offsetHeight;
        set(1 - ((ev.clientY - r.top) / u - cap.offsetHeight / 2) / h);
      } else {
        const w = (r.width / u) - cap.offsetWidth;
        set(((ev.clientX - r.left) / u - cap.offsetWidth / 2) / w);
      }
    };
    mv(e);
    el.addEventListener('pointermove', mv);
    el.addEventListener('pointerup', () => el.removeEventListener('pointermove', mv), { once: true });
  });
  el.addEventListener('dblclick', () => set(value));   // ダブルクリックで初期位置に戻す
  requestAnimationFrame(place);
  addEventListener('resize', place);
  return Object.assign(self, { set, get: () => v });
}
