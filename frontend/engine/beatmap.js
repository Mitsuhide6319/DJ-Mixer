// 時刻 ⇔ 拍位置の変換（design_phase1.md §4.2）。
// 拍位置は「拍の番号＋拍内の割合」の実数。拍と拍の間は線形補間、曲頭・曲末は端の拍間隔で外挿する。
// テンポが揺れる曲でも、拍位置で比べれば 2 曲の拍を合わせられる（Phase 0 で検証した方式）。

function lowerBound(arr, v) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; }
  return lo;
}

export class BeatMap {
  /** analysis: /api/tracks/{id}/analysis の結果（beats, bars, meter, no_beat）。 */
  constructor(analysis) {
    this.beats = analysis.beats;
    this.meter = analysis.meter;
    this.bars = analysis.bars;                          // [{beat, time, conf}]
    this.barBeats = this.bars.map(b => b.beat);
    this.noBeat = analysis.no_beat;
    this.n = this.beats.length;
  }

  get usable() { return this.n >= 8; }

  pos(t) {
    const b = this.beats, n = this.n;
    if (t <= b[0]) return (t - b[0]) / (b[1] - b[0]);
    if (t >= b[n - 1]) return n - 1 + (t - b[n - 1]) / (b[n - 1] - b[n - 2]);
    const i = lowerBound(b, t) - 1;
    return i + (t - b[i]) / (b[i + 1] - b[i]);
  }

  time(p) {
    const b = this.beats, n = this.n;
    if (p <= 0) return b[0] + p * (b[1] - b[0]);
    if (p >= n - 1) return b[n - 1] + (p - (n - 1)) * (b[n - 1] - b[n - 2]);
    const i = Math.floor(p);
    return b[i] + (p - i) * (b[i + 1] - b[i]);
  }

  /** その位置の拍間隔（秒）。 */
  beatLen(t) {
    const i = Math.max(0, Math.min(this.n - 2, lowerBound(this.beats, t) - 1));
    return this.beats[i + 1] - this.beats[i];
  }

  /** SYNC に使えない区間（拍なし・テンポ不安定）か。 */
  inNoBeat(t) { return this.noBeat.some(([s, e]) => t >= s && t < e); }

  /** 拍位置 p が小節の中で何拍目か（0 始まりの実数）と、その小節の拍数。小節情報が無ければ拍子で割る。 */
  beatInBar(p) {
    const k = lowerBound(this.barBeats, Math.floor(p) + 1) - 1;
    if (k < 0) return { beat: ((p % this.meter) + this.meter) % this.meter, len: this.meter };
    const start = this.barBeats[k], next = this.barBeats[k + 1] ?? start + this.meter;
    return { beat: p - start, len: next - start };
  }
}
