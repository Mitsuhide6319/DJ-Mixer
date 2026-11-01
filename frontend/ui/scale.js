// 操作パネルの拡大率 --u（design_phase1.md §5.1）。1920×1080 基準、0.85〜1.5 倍。
// CSS の zoom はレイアウトごと拡大するので文字・SVG がぼやけない。

export const uiScale = () =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--u')) || 1;

export function applyScale() {
  // 上部（ヘッダー・曲情報行）も拡大されるので、2 回計算して収束させる
  for (let i = 0; i < 2; i++) {
    const topH = document.querySelector('.top').getBoundingClientRect().height
      + document.querySelector('.strip').getBoundingClientRect().height;
    const u = Math.max(0.85, Math.min(1.5, innerWidth / 1920, (innerHeight - topH) / 745));
    document.documentElement.style.setProperty('--u', u.toFixed(3));
  }
}

export function initScale() {
  addEventListener('resize', applyScale);
  applyScale();
}
