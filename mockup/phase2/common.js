// Phase 2 モックアップ共通（案A・B・C）。本番の frontend/ui/waveform.js と同じ描き方（輪郭の塗りつぶし・実ピクセル座標）。
(function () {
  const T = window.MOCK2;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const C = {};
  for (const n of ['--deck-a', '--deck-b', '--wave-low', '--wave-mid', '--wave-high', '--line', '--dim', '--faint', '--active', '--warn', '--ok', '--text'])
    C[n] = css(n);

  // 区間の性格の色（「いま効いているもの」に使う赤は避ける）
  const SEC_COLOR = { '静か': '#2dd4bf', '中くらい': '#94a3b8', '盛り上がり': '#fb923c' };
  const secKind = s => s.tags.find(t => SEC_COLOR[t]) ?? '中くらい';

  for (const t of T) {
    t.barTimes = t.bars.map(b => b[1]);
    t.use = { t0: 0, t1: t.duration };
    t.wave.low = Uint8Array.from(t.wave.low); t.wave.mid = Uint8Array.from(t.wave.mid); t.wave.high = Uint8Array.from(t.wave.high);
  }

  function fmt(s, dec = 1) {
    s = Math.max(0, s);
    const m = Math.floor(s / 60), r = s - m * 60;
    return `${m}:${r.toFixed(dec).padStart(dec ? 3 + dec : 2, '0')}`;
  }
  function nearest(arr, v) {
    let lo = 0, hi = arr.length - 1;
    if (v <= arr[0]) return 0;
    if (v >= arr[hi]) return hi;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (arr[m] <= v) lo = m; else hi = m; }
    return (v - arr[lo] < arr[hi] - v) ? lo : hi;
  }
  /** 小節 / 拍 / 自由（0.1 秒）への吸着。 */
  function snap(t, time, mode) {
    if (mode === 'bar') { const i = nearest(t.barTimes, time); return t.barTimes[i]; }
    if (mode === 'beat') { const i = nearest(t.beats, time); return t.beats[i]; }
    return Math.round(time * 10) / 10;
  }
  const barIndex = (t, time) => nearest(t.barTimes, time);
  const barsBetween = (t, a, b) => barIndex(t, b) - barIndex(t, a);

  function fitCanvas(cv) {
    const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * d)), h = Math.max(1, Math.round(r.height * d));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const g = cv.getContext('2d');
    g.setTransform(1, 0, 0, 1, 0, 0);
    return [g, w, h, d];
  }
  function sampleAt(arr, f0, f1) {
    if (f1 - f0 < 1) {
      const f = (f0 + f1) / 2, i = Math.floor(f), u = f - i;
      if (i < 0 || i + 1 >= arr.length) return 0;
      return arr[i] * (1 - u) + arr[i + 1] * u;
    }
    let v = 0; const a = Math.max(0, Math.floor(f0)), b = Math.min(arr.length, Math.ceil(f1));
    for (let i = a; i < b; i++) if (arr[i] > v) v = arr[i];
    return v;
  }
  function fillEnv(g, x0, vals, mid, amp, color) {
    const n = vals.length;
    g.beginPath(); g.moveTo(x0, mid);
    for (let i = 0; i < n; i++) g.lineTo(x0 + i, mid - vals[i] * amp);
    for (let i = n - 1; i >= 0; i--) g.lineTo(x0 + i, mid + vals[i] * amp);
    g.closePath(); g.fillStyle = color; g.fill();
  }
  /** 曲 t の [ta, tb] 秒を、実ピクセル x0..x1、縦 y0..y0+h に描く。alpha で薄くできる。 */
  function drawWave(g, t, ta, tb, x0, x1, y0, h, alpha = 1) {
    const W = Math.max(1, Math.round(x1 - x0)), rate = t.wave.rate, mid = y0 + h / 2;
    g.save();
    g.globalAlpha = alpha;
    for (const [band, col, sc] of [['low', C['--wave-low'], 1], ['mid', C['--wave-mid'], 0.72], ['high', C['--wave-high'], 0.42]]) {
      const v = new Float32Array(W), arr = t.wave[band];
      for (let x = 0; x < W; x++) {
        const f0 = (ta + (tb - ta) * x / W) * rate, f1 = (ta + (tb - ta) * (x + 1) / W) * rate;
        v[x] = sampleAt(arr, f0, f1);
      }
      const s = new Float32Array(W);
      for (let x = 0; x < W; x++) s[x] = (v[Math.max(0, x - 1)] + 2 * v[x] + v[Math.min(W - 1, x + 1)]) / 4;
      fillEnv(g, Math.round(x0), s, mid, (h / 2 - 2) / 255 * sc, col);
    }
    g.restore();
  }
  /** 小節線（と 8 小節ごとの番号）。xOf(time) で位置を決める。 */
  function drawBars(g, t, ta, tb, xOf, y0, h, d, every = 8, label = true) {
    for (let i = 0; i < t.barTimes.length; i++) {
      const bt = t.barTimes[i];
      if (bt < ta - 0.01 || bt > tb + 0.01) continue;
      const x = Math.round(xOf(bt)), major = i % every === 0;
      g.fillStyle = major ? 'rgba(255,255,255,.35)' : 'rgba(255,255,255,.1)';
      g.fillRect(x, y0, major ? d : d, h);
      if (label && major) { g.fillStyle = C['--dim']; g.font = `${10 * d}px Consolas`; g.fillText(String(i + 1), x + 3 * d, y0 + 11 * d); }
    }
  }
  function applyScale() {
    const u = Math.max(0.85, Math.min(1.5, innerWidth / 1920, innerHeight / 1080));
    document.documentElement.style.setProperty('--u', u.toFixed(3));
  }
  addEventListener('resize', applyScale); applyScale();
  const uiScale = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--u')) || 1;

  window.P2 = { T, C, SEC_COLOR, secKind, fmt, snap, barIndex, barsBetween, fitCanvas, drawWave, drawBars, nearest, uiScale };
})();
