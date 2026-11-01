// 小窓（Phase 3: まとめて解析の範囲・おまかせの確認・書き出し）。背景のクリック・Esc・[data-close] で閉じる。
let cur = null;

export function closeModal() {
  cur?.el.remove();
  cur?.onClose?.();
  cur = null;
}

/** html を小窓に出し、bind(el) で操作を付ける。戻り値は小窓の要素。 */
export function openModal(html, bind, onClose) {
  closeModal();
  const el = document.createElement('div');
  el.className = 'mback';
  el.innerHTML = `<div class="mbox">${html}</div>`;
  document.body.appendChild(el);
  el.addEventListener('click', e => { if (e.target === el || e.target.closest('[data-close]')) closeModal(); });
  cur = { el, onClose };
  bind?.(el);
  return el;
}

addEventListener('keydown', e => { if (e.key === 'Escape' && cur) { e.stopPropagation(); closeModal(); } }, true);
