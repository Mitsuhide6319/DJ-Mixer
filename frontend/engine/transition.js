// 繋ぎの計算（design_phase2.md §3）。純粋な関数。つなぎ目エディタの線の描画と、実際の再生（runner.js）の両方が使う。
//
// 重なりの中の位置 u（0..1）は、前の曲（出ていく曲）の拍位置で数える。テンポが揺れても、低音の入れ替えが狙った小節で起きる。
// 返す値:
//   xf      クロスフェーダーの位置（0 = 前の曲だけ, 1 = 次の曲だけ）
//   lowOut  前の曲の LOW の量（1 = そのまま, 0 = 消す）
//   lowIn   次の曲の LOW の量
//   hpOut   前の曲にかけるハイパスの量（0 = かけない, 1 = 強く）
//   echoOut 前の曲のエコーの送り（0..1）

export const TEMPLATES = {
  long:   { label: 'ロングブレンド（低音を入れ替えてゆっくり）', bars: 16 },
  short:  { label: 'ショートブレンド（短く重ねる）', bars: 8 },
  bass:   { label: '低音だけ入れ替え（両方鳴らしたまま）', bars: 16 },
  filter: { label: 'フィルターで前の曲を消す', bars: 16 },
  echo:   { label: 'エコーアウト（前の曲を響かせて切り替え）', bars: 2 },
  cut:    { label: 'カットイン（小節の頭でスパッと切り替え）', bars: 0 },
};
// tempo: 'sync'（次の曲を合わせる）/ 'ramp'（テンポランプ）、loop: 前の曲の最後の N 小節を繰り返す（0/4/8）、shift: 次の曲を半音ずらす（Phase 4）
// parts: パートごとに鳴らす（Phase 5 B2）'none' / 'drums'（次の曲はドラムから）/ 'vocal'（前の曲の歌を先に抜く）/ 'swap'（両方）
// cutTrim: カットインで次の曲の入りに掛ける音量（'auto' = 自動で前の曲の終わりにそろえる / 'off' / dB の数。design_cutin.md）
export const DEFAULT_PARAMS = { swapAt: 0.5, xfStart: 0, xfEnd: 1, tempo: 'sync', loop: 0, shift: 0, parts: 'none', cutTrim: 'auto' };
export const PARTS = {
  none:  { label: 'パートは分けない' },
  drums: { label: 'ドラムから入れる', hint: '次の曲はドラム→その他→ベース（入れ替えの位置）→歌の順に入る' },
  vocal: { label: '歌を先に抜く', hint: '前の曲の歌を重なりの最初の 1/4 で消す' },
  swap:  { label: '入れ替え（両方）', hint: '前の曲の歌を先に抜き、次の曲はドラムから。ベースは入れ替えの位置で交代' },
};
export const PART_NAMES = ['ボーカル', 'ドラム', 'ベース', 'その他'];
/** パートごとに鳴らす繋ぎか（拍を合わせて重ねる繋ぎ方のときだけ）。 */
export const usesParts = join => !!join && ['long', 'short', 'bass'].includes(join.tpl) && !!PARTS[join.params?.parts] && join.params.parts !== 'none';
export const DEFAULT_JOIN = { bars: 16, tpl: 'long', params: DEFAULT_PARAMS };

/** 保存形式（queue_items の join_*）から、欠けている値を標準で補った繋ぎを作る。 */
export function joinOf(item) {
  const tpl = TEMPLATES[item?.join_tpl] ? item.join_tpl : 'long';
  const bars = item?.join_bars ?? TEMPLATES[tpl].bars;
  return { tpl, bars: tpl === 'cut' ? 0 : Math.max(1, Math.min(64, bars)), params: { ...DEFAULT_PARAMS, ...(item?.join_params ?? {}) } };
}

const clamp01 = v => Math.max(0, Math.min(1, v));
/** a から b へ、幅 w の中で滑らかに切り替える（0 → 1）。 */
const step = (u, at, w) => (w <= 0 ? (u >= at ? 1 : 0) : clamp01((u - at) / w + 0.5));

/**
 * 重なりの中の位置 u における各操作子の値。
 * beats: 重なりの拍数（低音の入れ替えを 1 拍かけて行うために使う）。
 */
export function levels(join, u, beats = 64) {
  const { tpl, params: p } = join;
  u = clamp01(u);
  const oneBeat = 1 / Math.max(1, beats);
  // パートごとに鳴らすときは、低音の入れ替えをベースのパートで行うので EQ の LOW は動かさない（Phase 5 B2）
  const neutral = usesParts(join);
  const swapOut = neutral ? 1 : 1 - step(u, p.swapAt, oneBeat), swapIn = neutral ? 1 : step(u, p.swapAt, oneBeat);
  const lastBar = Math.min(1, 4 * oneBeat);
  switch (tpl) {
    case 'bass':
      return { xf: u < 1 - lastBar ? 0.5 : 0.5 + 0.5 * clamp01((u - (1 - lastBar)) / lastBar), lowOut: swapOut, lowIn: swapIn, hpOut: 0, echoOut: 0 };
    case 'filter':
      return { xf: u < 0.8 ? 0.5 : 0.5 + 0.5 * clamp01((u - 0.8) / 0.2), lowOut: 1 - u, lowIn: clamp01(u * 2), hpOut: u * 0.9, echoOut: 0 };
    case 'echo':
      // 最後の 1 小節でエコーを上げ、最後の半拍で次の曲へ切り替える。余韻はフェーダーの前から分岐しているので残る
      return { xf: u < 1 - oneBeat / 2 ? 0 : 1, lowOut: 1, lowIn: 1, hpOut: 0, echoOut: clamp01((u - (1 - lastBar)) / lastBar) };
    case 'cut':
      return { xf: u < 1 ? 0 : 1, lowOut: 1, lowIn: 1, hpOut: 0, echoOut: 0 };
    case 'long':
    case 'short':
    default: {
      const span = Math.max(1e-6, p.xfEnd - p.xfStart);
      return { xf: clamp01((u - p.xfStart) / span), lowOut: swapOut, lowIn: swapIn, hpOut: 0, echoOut: 0 };
    }
  }
}

/**
 * パートごとの音量（Phase 5 B2）。戻り値 {out: [ボーカル, ドラム, ベース, その他], in: [...]}（0..1）、パートを使わない繋ぎなら null。
 * 前の曲は重なりの始まりではすべて 1、次の曲は重なりの終わりですべて 1（そこで元の音に戻すので、切り替わりが聞こえない）。
 */
export function partGains(join, u, beats = 64) {
  if (!usesParts(join)) return null;
  const p = join.params, mode = p.parts;
  u = clamp01(u);
  const oneBeat = 1 / Math.max(1, beats), bar = Math.min(0.25, 4 * oneBeat);
  const out = [1, 1, 1, 1], inn = [1, 1, 1, 1];
  if (mode === 'vocal' || mode === 'swap') out[0] = 1 - clamp01(u / 0.25);                       // 歌を最初の 1/4 で消す
  if (mode === 'swap') out[2] = 1 - step(u, p.swapAt, oneBeat);                                   // ベースは入れ替えの位置で交代
  if (mode === 'drums' || mode === 'swap') {
    inn[3] = clamp01((u - 0.25) / Math.max(bar, 0.125));                                         // その他は 1/4 から
    inn[2] = step(u, p.swapAt, oneBeat);                                                          // ベースは入れ替えの位置で
    inn[0] = clamp01((u - 0.75) / 0.25);                                                          // 歌は最後の 1/4 で
  }
  return { out, in: inn };
}

/** LOW の量（0..1）をミキサーの EQ つまみの値へ（0.5 がそのまま、0 が消す）。 */
export const lowKnob = level => 0.5 * level;
/** ハイパスの量（0..1）をフィルターつまみの値へ（0.5 が素通し、右へ行くほどハイパス）。 */
export const filterKnob = hp => 0.5 + 0.5 * hp;

/**
 * 重なりの位置: 前の曲の使う範囲の終わり outEnd（秒）から bars 小節前の小節頭。
 * barTimes: 前の曲の小節頭の時刻。戻り値 {startTime, startBar, endBar}。
 */
export function overlapStart(barTimes, outEnd, bars) {
  let endBar = 0;
  for (let i = 0; i < barTimes.length; i++) if (Math.abs(barTimes[i] - outEnd) < Math.abs(barTimes[endBar] - outEnd)) endBar = i;
  const startBar = Math.max(0, endBar - bars);
  return { startTime: barTimes[startBar], startBar, endBar };
}

/**
 * 2 曲を拍を合わせて繋げるか（design_phase2.md §3「自動で繋ぎ方を変える場合」）。つなぎ目エディタの表示と runner.js の両方が使う。
 * bmOut/bmIn: BeatMap、ovStart/ovEnd: 前の曲の重なり（秒）、inStart: 次の曲の入り口（秒）、rateOut: 前の曲の再生速度。
 * 戻り値 {k, rate, reason}: k は次の曲のテンポの倍率（1/2/½）、rate は次の曲に必要な速度、reason は合わせられない理由（無ければ null）。
 */
export function judge(bmOut, bmIn, ovStart, ovEnd, inStart, rateOut = 1) {
  const bpmOut = 60 / bmOut.beatLen(ovStart) * rateOut;
  const bpmIn = 60 / bmIn.beatLen(inStart);
  let best = null;
  for (const k of [1, 2, 0.5]) { const rate = k * bpmOut / bpmIn; if (!best || Math.abs(Math.log(rate)) < Math.abs(Math.log(best.rate))) best = { k, rate }; }
  let reason = null;
  if (best.rate < 0.7 || best.rate > 1.3) reason = `テンポの差が大きすぎます（${Math.round((best.rate - 1) * 100)}%）`;
  else {
    for (let t = ovStart; t < ovEnd; t += 0.25) if (bmOut.inNoBeat(t)) { reason = '前の曲の繋ぐ部分に拍がありません'; break; }
    const inLen = ovEnd - ovStart;
    if (!reason) for (let t = inStart; t < inStart + Math.max(inLen, 2); t += 0.25) if (bmIn.inNoBeat(t)) { reason = '次の曲の入る部分に拍がありません'; break; }
  }
  return { ...best, reason };
}

/**
 * 繋ぐ部分が拍の取れる所になるよう、使う範囲の終わり（end）・始まり（start）をずらす（Phase 5 F。design_phase5.md §F）。
 * planner/desc.py の stable_bounds と同じ規則: ずらすのは最大 max(90 秒, 使う範囲の 40%)、使う範囲は 2 分以上残す。
 * noBeat: 拍の取れない区間 [[s, e]]、barTimes: 小節頭（末尾に曲の長さを足したもの）、b0/b1: 使う範囲の小節番号、R: 繋ぐ部分の小節数。
 * 戻り値 {b0, b1}（見つからなければそのまま）。
 */
export function stableBounds(noBeat, barTimes, b0, b1, R, { end = true, start = true, tol = 1 } = {}) {
  const bt = barTimes, nb = (s, e) => noBeat.reduce((a, [x, y]) => a + Math.max(0, Math.min(bt[e], y) - Math.max(bt[s], x)), 0);
  const ok = (s, e) => nb(s, e) < tol;     // tol: 許す拍なしの秒（おまかせは 1 秒。つなぎ目エディタは再生の判定 judge に合わせてほぼ 0）
  const limit = Math.max(90, 0.4 * (bt[b1] - bt[b0]));
  let n0 = b0, n1 = b1;
  if (end && b1 - b0 > R && !ok(b1 - R, b1)) {
    for (let e = b1 - 1; e >= b0 + R; e--) {
      if (bt[b1] - bt[e] > limit || bt[e] - bt[b0] < 120) break;
      if (ok(e - R, e)) { n1 = e; break; }
    }
  }
  if (start && n1 - b0 > R && !ok(b0, b0 + R)) {
    for (let s = b0 + 1; s <= n1 - R; s++) {
      if (bt[s] - bt[b0] > limit || bt[n1] - bt[s] < 120) break;
      if (ok(s, s + R)) { n0 = s; break; }
    }
  }
  return { b0: n0, b1: n1 };
}

/** 次の曲を前の曲の終わりで入れる繋ぎ方か（エコーアウト・カット）。それ以外は重なりの始まりから重ねる。 */
export const entersAtEnd = tpl => tpl === 'echo' || tpl === 'cut';

/** 次の曲の入り口: 使う範囲の頭に最も近い小節頭（拍の端数でずれないように。P2-5 の試験で見つけた）。 */
export function inStartBar(barTimes, t0) {
  let b = 0;
  for (let i = 1; i < barTimes.length; i++) if (Math.abs(barTimes[i] - t0) < Math.abs(barTimes[b] - t0)) b = i;
  return b;
}

/**
 * 繋ぎの終わりの、次の曲の速度（前の曲に拍を合わせていた速度）。直前 4 拍（前の曲の拍で数える）の平均で求める。
 * SYNC がその瞬間に使っている速度は位相を寄せる動きで揺れるので、原速へ戻す起点には使わない（P3-8 で、揺れた 0.87 が
 * テンポフェーダーの幅で 0.92 に止まり、2 曲目が 1 秒遅れたのを見つけた）。高速書き出し（djmix/export/transition.py）も同じ式。
 */
export function syncedRate(bmIn, bmOut, posIn, posOut, k, rateOut = 1) {
  const pI = bmIn.pos(posIn), pO = bmOut.pos(posOut);
  const inSec = bmIn.time(pI) - bmIn.time(pI - 4 * k);
  const outSec = bmOut.time(pO) - bmOut.time(pO - 4);
  return outSec > 0 ? inSec * rateOut / outSec : 1;
}

/**
 * 重なりの小節（Phase 4 のループを含む）。前の曲の使う範囲の終わり outEnd に最も近い小節頭で終わり、重なりは join.bars 小節。
 * ループ（loop 小節）があれば、前の曲は (bars − loop) 小節ぶん進んだあと、最後の loop 小節を 1 回繰り返す。
 * エコーアウト・カットはループしない。戻り値 {startBar, endBar, startTime, loop, loopStartBar}。runner・つなぎ目エディタ・高速書き出しで共通。
 */
export function overlapBars(barTimes, outEnd, join) {
  const loop = join.tpl === 'echo' || join.tpl === 'cut' ? 0 : Math.max(0, Math.min(join.params?.loop ?? 0, join.bars - 1));
  const ov = overlapStart(barTimes, outEnd, join.bars - loop);
  return { ...ov, loop, loopStartBar: ov.endBar - loop };
}

/** テンポランプの終わりの前の曲の速度: 次の曲がちょうど原速になる速度（前後の 4 拍の平均のテンポの比）。 */
export function rampTarget(bmOut, bmIn, posOutEnd, posInEnd, k) {
  const pO = bmOut.pos(posOutEnd), pI = bmIn.pos(posInEnd);
  const outSec = bmOut.time(pO) - bmOut.time(pO - 4), inSec = bmIn.time(pI) - bmIn.time(pI - 4 * k);
  return inSec > 0 ? outSec / inSec : 1;
}

/** Camelot 表記を s 半音ずらす（1 半音で円の上を 7 つ進む。長調・短調は変わらない）。 */
export function shiftCamelot(c, s) {
  if (!c || !s) return c;
  const n = parseInt(c, 10), l = c.slice(-1);
  return `${(((n - 1 + 7 * s) % 12) + 12) % 12 + 1}${l}`;
}

// ---- カットインの音量合わせ（design_cutin.md ③。djmix/analysis/cutpoints.py と同じ式） ----
export const TRIM_MAX_DB = 6, TRIM_DEAD_DB = 1, LEVEL_BARS = 4, TRIM_BACK_BARS = 16;
/** [t0, t1) に頭がある小節の音量（dB）。bars: [{t, db}]。無ければ null。 */
export function levelDb(bars, t0, t1) {
  let s = 0, n = 0;
  for (const b of bars ?? []) if (b.t >= t0 - 1e-3 && b.t < t1 - 1e-3) { s += Math.pow(10, b.db / 10); n++; }
  return n ? 10 * Math.log10(s / n) : null;
}
/** 曲ごとの音量の揃え（mixer.js の setLoudness と同じ上限）。 */
export const loudDb = (lufs, target) => Math.max(-12, Math.min(9, target - lufs));
/** 自動の音量合わせ: 前の曲の最後の 4 小節と次の曲の最初の 4 小節の、聞こえる大きさの差（dB。±6 まで、1 未満は 0）。 */
export function autoTrimDb(barsOut, tCut, barOut, lufsOut, barsIn, tIn, barIn, lufsIn, target) {
  const lo = levelDb(barsOut, tCut - LEVEL_BARS * barOut, tCut), li = levelDb(barsIn, tIn, tIn + LEVEL_BARS * barIn);
  if (lo == null || li == null) return 0;
  const d = (lo + loudDb(lufsOut, target)) - (li + loudDb(lufsIn, target));
  if (Math.abs(d) < TRIM_DEAD_DB) return 0;
  return Math.round(Math.max(-TRIM_MAX_DB, Math.min(TRIM_MAX_DB, d)) * 10) / 10;
}
/** 繋ぎの設定 cutTrim から実際に掛ける dB。 */
export function trimDb(params, auto) {
  const v = params?.cutTrim ?? 'auto';
  if (v === 'off') return 0;
  if (typeof v === 'number') return Math.max(-TRIM_MAX_DB, Math.min(TRIM_MAX_DB, v));
  return auto;
}
