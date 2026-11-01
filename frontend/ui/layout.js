// タブ切替とミックスキューの表示/非表示（design_phase1.md §5.3、打合せ9）。
// キューの表示状態は設定として保存し、次回起動時も引き継ぐ。

export function initTabs() {
  document.querySelectorAll('.tab:not(.dis)').forEach(t => t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v.id === t.dataset.v));
  }));
}

const mainEl = () => document.querySelector('.main');

export function isQueueVisible() { return !mainEl().classList.contains('qhidden'); }

export function setQueueVisible(show, persist = true) {
  mainEl().classList.toggle('qhidden', !show);
  document.getElementById('qlabel').textContent = show ? 'ミックスキューを隠す ▸' : '◂ ミックスキューを表示';
  // 表示状態は設定として保存し、次回起動時も引き継ぐ（P1-10）
  if (persist) onQueueVisible?.(show);
}

let onQueueVisible = null;

export function toggleQueue() { setQueueVisible(!isQueueVisible()); }

export function initQueueToggle(initial, onChange) {
  onQueueVisible = onChange;
  document.getElementById('qtoggle').addEventListener('click', toggleQueue);
  setQueueVisible(initial !== false, false);
}
