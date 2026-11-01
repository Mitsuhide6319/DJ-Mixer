// 波形描画（design_phase1.md §5.4）。キャンバスは実ピクセル解像度で確保し、座標も実ピクセルで計算する。
// 波形は縦棒の並びではなく上下対称の輪郭を 1 本のパスで塗る（エッジにアンチエイリアスが効く）。

/** キャンバスを devicePixelRatio 倍の実ピクセルで確保し、[ctx, 幅, 高さ, dpr] を返す。 */
export function fitCanvas(cv) {
  const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  // zoom 下でも getBoundingClientRect は画面上の大きさを返すので、そのまま実ピクセルになる
  const w = Math.max(1, Math.round(r.width * d)), h = Math.max(1, Math.round(r.height * d));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const g = cv.getContext('2d');
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.imageSmoothingEnabled = true;
  return [g, w, h, d];
}

/** 波形配列 arr の [f0, f1) 区間の値。1 点未満なら線形補間、それ以上なら区間最大値。 */
export function sampleAt(arr, f0, f1) {
  if (f1 - f0 < 1) {
    const f = (f0 + f1) / 2, i = Math.floor(f), u = f - i;
    if (i < 0 || i + 1 >= arr.length) return 0;
    return arr[i] * (1 - u) + arr[i + 1] * u;
  }
  let v = 0;
  const a = Math.max(0, Math.floor(f0)), b = Math.min(arr.length, Math.ceil(f1));
  for (let i = a; i < b; i++) if (arr[i] > v) v = arr[i];
  return v;
}

export function smooth3(v) {
  const o = new Float32Array(v.length), n = v.length;
  for (let i = 0; i < n; i++) o[i] = (v[Math.max(0, i - 1)] + 2 * v[i] + v[Math.min(n - 1, i + 1)]) / 4;
  return o;
}

/** 上下対称の輪郭を塗る。vals は 0..255。 */
export function fillEnvelope(g, vals, mid, amp, color) {
  const n = vals.length;
  g.beginPath();
  g.moveTo(0, mid);
  for (let x = 0; x < n; x++) g.lineTo(x, mid - vals[x] * amp);
  for (let x = n - 1; x >= 0; x--) g.lineTo(x, mid + vals[x] * amp);
  g.closePath();
  g.fillStyle = color;
  g.fill();
}

export function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

/** 帯域別の描画順と縦方向の縮尺（低域を一番大きく、高域を手前に小さく重ねる）。 */
export const BAND_LAYERS = [['low', '--wave-low', 1.0], ['mid', '--wave-mid', 0.72], ['high', '--wave-high', 0.42]];

/** 全体波形。wave = {rate, low, mid, high}（各 Uint8Array）。 */
export function drawOverview(cv, wave, pos01, colors, playColor) {
  const [g, W, H, d] = fitCanvas(cv);
  g.clearRect(0, 0, W, H);
  if (!wave) return;
  const n = wave.low.length;
  for (const [band, cssVar, sc] of BAND_LAYERS) {
    const arr = wave[band], v = new Float32Array(W);
    for (let x = 0; x < W; x++) v[x] = sampleAt(arr, x / W * n, (x + 1) / W * n);
    fillEnvelope(g, smooth3(v), H / 2, (H / 2 - 2 * d) / 255 * sc, colors[cssVar]);
  }
  const px = Math.round(pos01 * W);
  g.fillStyle = 'rgba(0,0,0,.55)';
  g.fillRect(0, 0, px, H);
  g.fillStyle = playColor;
  g.fillRect(px - d, 0, 2 * d, H);
}
