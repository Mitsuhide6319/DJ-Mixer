// 繋ぎの実行（design_phase2.md §4）。「つなぎ目だけ試聴」と「ミックスを通して再生」で共用する。
//
// 1 つの繋ぎ（前の曲 = out デッキ、次の曲 = in デッキ）の流れ:
//   pre      前の曲だけが鳴っている。重なりの始まりが来たら、次の曲を位相を固定した SYNC で鳴らし始める
//   overlap  重なりの中。前の曲の拍位置から u（0..1）を求め、transition.js の値をミキサーに設定する（20ms ごと）
//   post     前の曲を止めた後。次の曲の速度を 8 小節かけて原速へ戻す
// 同期できない組み合わせ（テンポ差 ±30% 超・拍なし区間）はエコーアウトに切り替え、前の曲の終わりで次の曲を原速で始める。
// 自動で動かしている操作子をユーザーが動かしたら、その操作子だけ手動に任せる（テイクオーバー、§4.3）。
import * as actions from '../input/actions.js';
import { joinOf, levels, lowKnob, filterKnob, overlapStart, judge, inStartBar, entersAtEnd, syncedRate, overlapBars, rampTarget, usesParts, partGains,
         autoTrimDb, trimDb, TRIM_BACK_BARS } from './transition.js';

const START_EARLY = 0.06;     // 次の曲を鳴らし始める判定の前倒し（刻みが 20ms なので少し早めに判定し、位相は SYNC がそろえる）
// 前の曲の終わりで次の曲を入れる繋ぎ（カットイン・エコーアウト）は、この秒数前に「前の曲が小節の頭に来る出力時刻」を求めて、
// 次の曲の再生開始とクロスフェーダーの切り替えをその時刻に予約する（20ms の刻みに左右されず、サンプル単位でそろう。design_cutin.md ①）
const LATE_LEAD = 0.3;
const TAKEOVER = 0.02;        // 自動で設定した値からこれ以上ずれていたら、ユーザーが動かしたとみなす
const RETURN_BARS = 8;        // 原速へ戻すのにかける小節数

const sleep = ms => new Promise(r => setTimeout(r, ms));

export class Runner {
  /** o: {engine, sync, trackData, useOf, loadDeck, getItems, settings(), flash, onChange, markPlayed(trackId)} */
  constructor(o) {
    Object.assign(this, o);
    this.tr = null;          // 実行中の繋ぎ
    this.mode = null;        // 'preview' | 'mix'
    this.saved = null;       // 試聴前のミキサーの状態（試聴が終わったら戻す）
    this.sync.onTick(() => { try { this.tick(); } catch (e) { console.error(e); this.stop(); } });
  }

  get busy() { return !!this.mode; }

  status() {
    const tr = this.tr;
    if (!this.mode) return null;
    return { mode: this.mode, pair: tr?.pair, phase: tr?.phase, u: tr?.u ?? 0, reason: tr?.reason, manual: tr?.manual ? [...tr.manual] : [],
             tpl: tr?.join?.tpl, bars: tr?.join?.bars, outTitle: tr?.itemOut?.track.title, inTitle: tr?.itemIn?.track.title, mixIndex: this.mixIndex };
  }

  /** 再生位置を、つなぎ目エディタの座標（前の曲の重なりの始まりから数えた小節数）で返す。 */
  playhead() {
    const tr = this.tr;
    if (!tr?.phase) return null;
    const O = this.engine.decks[tr.outId], I = this.engine.decks[tr.inId];
    if (tr.phase === 'post' || (tr.lateIn && I.playing)) {
      const bm = this.sync.bm[tr.inId];
      return { pair: tr.pair, bar: (tr.lateIn ? tr.bars : 0) + (bm.pos(I.position()) - bm.pos(tr.inTime)) / (bm.meter || 4) };
    }
    const bm = this.sync.bm[tr.outId];
    if (tr.phase === 'overlap' && tr.acc !== undefined) return { pair: tr.pair, bar: tr.acc / (bm.meter || 4) };   // ループ中も進み続ける
    return { pair: tr.pair, bar: (bm.pos(O.position()) - tr.pS) / (bm.meter || 4) };
  }

  // ---- 準備 ----
  /** デッキに曲を読み込み、使う範囲の終わりまで PCM が届き、拍の情報がそろうまで待つ。 */
  async prepareDeck(id, item, d) {
    const dk = this.engine.decks[id];
    if (dk.track?.id !== item.track.id) await this.loadDeck(id, item.track);
    const need = Math.min(this.useOf(item, d).t1, dk.duration || Infinity) - 0.5;
    for (let k = 0; k < 150 && (dk.loadedSec < need || !this.sync.bm[id]); k++) await sleep(100);
    if (!this.sync.bm[id]) throw new Error(`「${item.track.title}」の解析結果を読み込めませんでした`);
    return dk;
  }

  /** 繋ぎの計画を立てる。 */
  plan(outId, inId, itemOut, itemIn, dOut, dIn, pair) {
    let join = joinOf(itemOut);
    const uo = this.useOf(itemOut, dOut), ui = this.useOf(itemIn, dIn);
    const bmOut = this.sync.bm[outId], bmIn = this.sync.bm[inId];
    const outDeck = this.engine.decks[outId];
    const mk = j => {
      const ov = overlapBars(dOut.barTimes, uo.t1, j);          // ループ（Phase 4）を含む
      return { ov, ovStart: ov.startTime, ovEnd: dOut.barTimes[ov.endBar] ?? uo.t1 };
    };
    let geo = mk(join);
    // 次の曲の入り口（使う範囲の頭に最も近い小節頭）。ここを前の曲の重なりの始まりの小節頭にぴったり重ねる。
    // 範囲の頭そのもの（例: 曲の最初の 0 秒）は拍の上に無いことがあり、そのまま合わせると拍が端数だけずれる
    // （P2-5 の試験で、次の曲の拍が一定して 160ms 遅れた）
    const inBar = inStartBar(dIn.barTimes, ui.t0);
    const inTime = dIn.barTimes[inBar] ?? ui.t0;
    // テンポの倍率 k（1/2/½ のうち速度が 1 に近いもの）と、拍を合わせて繋げるか
    const best = judge(bmOut, bmIn, geo.ovStart, geo.ovEnd, inTime, outDeck.rate || 1), reason = best.reason;
    const fellBack = !!reason && join.tpl !== 'echo' && join.tpl !== 'cut';
    if (fellBack) {
      join = { ...join, tpl: 'echo', bars: 2 };
      geo = mk(join);
    }
    const synced = !reason && join.tpl !== 'cut';
    // エコーアウト・カット・拍を合わせられない繋ぎは、次の曲を前の曲の終わりで入れる（合わせられるなら拍の上で）
    const lateIn = !synced || entersAtEnd(join.tpl);
    // カットイン（design_cutin.md）: 次の曲は小節の頭（1 拍目）から原速で。入りの音量を前の曲の終わりにそろえる（③）
    const cut = join.tpl === 'cut';
    let trimAuto = 0;
    if (cut) {
      const barO = bmOut.beatLen(geo.ovEnd) * (bmOut.meter || 4), barI = bmIn.beatLen(inTime) * (bmIn.meter || 4);
      trimAuto = autoTrimDb(dOut.cut?.bars, geo.ovEnd, barO, dOut.a.lufs ?? -14, dIn.cut?.bars, inTime, barI, dIn.a.lufs ?? -14,
                            this.engine.mixer.targetLufs ?? -14);
    }
    const trim = cut ? trimDb(join.params, trimAuto) : 0;
    const pS = bmOut.pos(geo.ovStart), pE = bmOut.pos(geo.ovEnd);
    const phi = bmIn.pos(inTime) - best.k * (lateIn ? pE : pS);
    // ループ（前の曲の最後の loop 小節を 1 回繰り返す）: 重なりの拍数に繰り返しの分を足す
    const loop = lateIn ? 0 : geo.ov.loop;
    const loopStart = loop ? dOut.barTimes[geo.ov.loopStartBar] : null;
    const loopBeats = loop ? pE - bmOut.pos(loopStart) : 0;
    const beats = Math.max(1, pE - pS + loopBeats);
    // テンポランプ: 重なりの終わりで次の曲がちょうど原速になるよう、前の曲の速度を変える
    const rampTo = !lateIn && join.params.tempo === 'ramp'
      ? rampTarget(bmOut, bmIn, geo.ovEnd, bmIn.time(bmIn.pos(inTime) + best.k * beats), best.k) : null;
    return {
      // パートごとに鳴らす（Phase 5 B2）: 拍を合わせて重ねる繋ぎだけ。分離した音がそろってから有効（partsReady）
      parts: !lateIn && usesParts(join) ? join.params.parts : null, partsReady: false,
      loop, loopStart, loopEnd: loop ? geo.ovEnd : null, loopBeats, rampTo, shift: join.params.shift || 0,
      pair, outId, inId, itemOut, itemIn, dOut, dIn, uo, ui, inTime, join, reason, synced, lateIn, k: best.k, phi, cut, trim, trimAuto, fellBack,
      ovStart: geo.ovStart, ovEnd: geo.ovEnd, startBar: geo.ov.startBar, bars: geo.ov.endBar - geo.ov.startBar + loop, pS, pE, beats,
      // 試聴の終わり: 次の曲が重なりの後さらに 8 小節鳴ったところ
      stopIn: dIn.barTimes[Math.min(dIn.barTimes.length - 1, inBar + (lateIn ? 0 : join.bars) + 8)] ?? ui.t1,
      phase: 'pre', u: 0, manual: new Set(), last: {},
    };
  }

  /** 繋ぎを始める直前の状態にする（次の曲は範囲の頭で待機、ミキサーは前の曲だけが聞こえる状態）。 */
  arm(tr) {
    const { outId, inId } = tr;
    const I = this.engine.decks[inId];
    this.sync.disable(inId, false);
    this.sync.disable(outId, true);
    this.sync.master = outId;
    if (I.playing) I.pause();
    I.seek(tr.synced || tr.cut ? tr.inTime : tr.ui.t0);     // 同期しない繋ぎは範囲の頭から（カットインは小節の頭から）
    this.engine.mixer.ch[inId].resetJoinGain();
    this.setRate(inId, 1);                        // 前の繋ぎの速度（テンポランプなど）を残さない
    I.setKeyShift(tr.shift);                      // キー合わせ（曲の終わりまで。Phase 4）
    this.set('mixer.crossfader', inId === 'B' ? 0 : 1, tr, true);
    for (const id of [outId, inId]) { this.set(`mixer.${id}.eq.low`, 0.5, tr, true); this.set(`mixer.${id}.filter`, 0.5, tr, true); }
    this.engine.mixer.ch[outId].setEcho(0);
    if (tr.synced) this.sync.armFixed(inId, tr.k, tr.phi);
    this.tr = tr;
  }

  /** 自動でミキサーの値を設定する。ユーザーが動かした操作子は、その繋ぎの間は触らない（テイクオーバー）。 */
  set(name, v, tr, force = false) {
    const a = actions.get(name);
    if (!a) return;
    if (!force) {
      if (tr.manual.has(name)) return;
      const last = tr.last[name];
      if (last !== undefined && a.get && Math.abs(a.get() - last) > TAKEOVER) {
        tr.manual.add(name);
        this.flash?.(`${label(name)} を手動に切り替えました（この繋ぎの間は自動で動かしません）`);
        this.onChange?.();
        return;
      }
    }
    if (a.get && Math.abs(a.get() - v) < 1e-4 && !force) { tr.last[name] = v; return; }
    actions.set(name, v);
    tr.last[name] = v;
  }

  apply(tr, vals) {
    const { outId, inId } = tr;
    this.set('mixer.crossfader', inId === 'B' ? vals.xf : 1 - vals.xf, tr);
    this.set(`mixer.${outId}.eq.low`, lowKnob(vals.lowOut), tr);
    this.set(`mixer.${inId}.eq.low`, lowKnob(vals.lowIn), tr);
    this.set(`mixer.${outId}.filter`, filterKnob(vals.hpOut), tr);
    const beatSec = this.sync.bm[outId].beatLen(this.engine.decks[outId].position()) / (this.engine.decks[outId].rate || 1);
    this.engine.mixer.ch[outId].setEcho(vals.echoOut, beatSec * 0.75);
  }

  // ---- 刻みごとの進行 ----
  tick() {
    const tr = this.tr;
    if (!tr) return;
    const now = this.engine.now;
    if (tr.phase === 'last') {          // ミックスの最後の曲: 使う範囲の終わりで止める
      const O = this.engine.decks[tr.outId];
      if (!O.playing) { this.stop(); return; }
      if (O.position(now) >= tr.t1 - 0.01) {
        O.pause();
        this.markPlayed?.(tr.itemOut.track.id);
        this.stop('ミックスを最後まで再生しました');
      }
      return;
    }
    const O = this.engine.decks[tr.outId], I = this.engine.decks[tr.inId];
    if (tr.phase === 'pre' || tr.phase === 'overlap') {
      if (!O.playing) { this.stop('前の曲が止められたので、繋ぎを中止しました'); return; }
      const posO = O.position(now);
      if (tr.lateIn && !tr.inAt && posO >= tr.ovEnd - LATE_LEAD * (O.rate || 1)) this.scheduleLateIn(tr, now, posO);
      if (tr.phase === 'pre' && posO >= tr.ovStart - START_EARLY) {
        if (!tr.lateIn) I.play();
        tr.phase = 'overlap';
        this.onChange?.();
      }
      // エコーアウト・カットは、前の曲の自動操作（エコー）を重なりの頭から進め、次の曲は前の曲の終わりで入れる
      // （P2-5 の試験で、エコーが掛からずに切り替わっていたのを直した）。入れる時刻は scheduleLateIn で予約済み
      if (tr.phase === 'overlap') {
        const bm = this.sync.bm[tr.outId];
        // 重なりの中の位置は、拍の進みを足し合わせて数える（ループで再生位置が巻き戻るため。Phase 4）
        const p = bm.pos(posO);
        if (tr.acc === undefined) { tr.acc = Math.max(0, p - tr.pS); tr.rampFrom = O.rate || 1; }
        else { let d = p - tr.lastP; if (d < -0.5 && tr.loop) d += tr.loopBeats; tr.acc += Math.max(0, d); }
        tr.lastP = p;
        // ループ: 前の曲が繰り返す小節に入ったらデッキのループを掛ける（SYNC は巻き戻りに付いていく）
        if (tr.loop && !tr.looping && posO >= tr.loopStart - 0.005) { O.setLoop(tr.loopStart, tr.loopEnd, tr.loopBeats); tr.looping = true; }
        tr.u = Math.max(0, Math.min(1, tr.acc / tr.beats));
        this.apply(tr, levels(tr.join, tr.u, tr.beats));
        // パートごとの音量（Phase 5 B2）。前の曲は重なりの頭（全パート 1）でパートの音へ切り替える
        if (tr.parts && !tr.partsReady && !tr.partsGaveUp) {
          tr.partsGaveUp = true;
          this.flash?.('パートの音が間に合わなかったので、この繋ぎはパートを分けずに繋ぎます');
        }
        if (tr.parts && tr.partsReady) {
          const g = partGains(tr.join, tr.u, tr.beats);
          if (!tr.outParts) { O.setPartsMode(true, g.out); tr.outParts = true; } else O.setPartGains(g.out);
          I.setPartGains(g.in);
        }
        // テンポランプ: 前の曲の速度を少しずつ変える（テンポフェーダーの幅にかかわらず。次の曲は SYNC で付いていく）
        if (tr.rampTo) {
          const r = tr.rampFrom + (tr.rampTo - tr.rampFrom) * tr.u;
          if (Math.abs(r - O.rate) > 0.0005) O.applyRate(r);
        }
        // 前の曲の終わりで入れる繋ぎは、予約した切り替えの時刻を過ぎてから（先に操作するとクロスフェーダーが早く動く）
        const ended = tr.lateIn ? !!tr.inAt && now >= tr.inAt + 0.005
          : tr.loop || tr.rampTo ? tr.acc >= tr.beats - 0.02 : posO >= tr.ovEnd - 0.01;
        if (ended && (!tr.lateIn || I.playing)) this.finishOverlap(tr);
      }
      return;
    }
    if (tr.phase === 'post') {
      if (!I.playing) { this.stop(); return; }
      const posI = I.position(now), bmI = this.sync.bm[tr.inId];
      if (tr.ramp) {
        const p = Math.max(0, Math.min(1, (bmI.pos(posI) - tr.ramp.start) / tr.ramp.beats));
        const rate = tr.ramp.from + (1 - tr.ramp.from) * p;
        if (Math.abs(rate - I.rate) > 0.0005 || p >= 1) {
          this.setRate(tr.inId, rate);
        }
        if (p >= 1) tr.ramp = null;
      }
      if (this.mode === 'preview' && posI >= tr.stopIn) { this.stop(); return; }
      if (this.mode === 'mix' && !tr.advancing) {
        // 原速へ戻し終えたら（または、戻している間に次の重なりが近づいたら）次の繋ぎを待機させる。
        // 次の曲は、前の曲を止めた時点で空いたデッキへ読み込み始めている（preload）
        const barSec = bmI.beatLen(posI) * (bmI.meter || 4) / (I.rate || 1);
        // 次の曲があるときだけ。最後の曲は原速へ戻し終えてから（P3-8 の比較で、最後の曲が合わせた速度のまま鳴り続けていたのを見つけた）
        const hasNext = !!this.getItems()[tr.pair + 2];
        const soon = hasNext && tr.preloaded && tr.ui.t1 - posI < (joinOf(tr.itemIn).bars + 4) * barSec;
        if (!tr.ramp || soon) {
          tr.advancing = true;
          this.nextJoin(tr.inId, tr.pair + 1).catch(e => { this.flash?.(String(e.message ?? e)); this.stop(); });
        }
      }
      // 次の繋ぎの準備が間に合わず、使う範囲の終わりを過ぎた
      if (this.mode === 'mix' && this.tr === tr && posI >= tr.ui.t1 - 0.01) {
        I.pause();
        this.markPlayed?.(tr.itemIn.track.id);
        this.stop(this.getItems()[tr.pair + 2] ? '次の曲の準備が間に合わなかったため、ここでミックスを止めました' : 'ミックスを最後まで再生しました');
      }
    }
  }

  /**
   * 前の曲の終わりで次の曲を入れる繋ぎ: 前の曲が小節の頭（ovEnd）に来る出力時刻を求め、その時刻に次の曲を鳴らし始める。
   * カットインは同じ時刻にクロスフェーダーを次の曲へ切り替え（前の曲はそこで聞こえなくなる）、入りの音量合わせも掛ける。
   */
  scheduleLateIn(tr, now, posO) {
    const O = this.engine.decks[tr.outId], I = this.engine.decks[tr.inId];
    const at = now + Math.max(0, tr.ovEnd - posO) / (O.rate || 1);
    I.play(at);
    tr.inAt = I.anchor.outT;                 // 実際に鳴り始める時刻（刻みが遅れて at が近すぎたときは少し後になる）
    if (tr.cut) {
      this.engine.mixer.scheduleXfade(tr.inId === 'B' ? 1 : 0, tr.inAt);
      if (tr.trim) {
        const bm = this.sync.bm[tr.inId];
        this.engine.mixer.ch[tr.inId].setJoinGainAt(tr.trim, tr.inAt, TRIM_BACK_BARS * bm.beatLen(tr.inTime) * (bm.meter || 4));
      }
    }
  }

  finishOverlap(tr) {
    const O = this.engine.decks[tr.outId], I = this.engine.decks[tr.inId];
    this.apply(tr, levels(tr.join, 1, tr.beats));
    if (tr.parts && tr.partsReady) I.setPartGains(partGains(tr.join, 1, tr.beats).in);
    O.pause();
    // パートの音をやめる（次の曲は全パート 1 になっているので、元の音へ戻しても聞こえ方は変わらない）
    O.clearStems();
    I.clearStems();
    if (tr.looping) O.exitLoop();
    if (this.mode === 'mix') {
      this.markPlayed?.(tr.itemOut.track.id);
      // 空いたデッキへ、キューのさらに次の曲を読み込み始める
      const next = this.getItems()[tr.pair + 2];
      if (next?.track.analysis_status === 'done') {
        this.trackData(next.track.id).then(d => this.prepareDeck(tr.outId, next, d))
          .then(() => { tr.preloaded = true; }, e => console.error(e));
      }
    }
    this.engine.mixer.ch[tr.outId].setEcho(0);      // 送りを止める（余韻はフィードバックで残る）
    this.set(`mixer.${tr.outId}.eq.low`, 0.5, tr, true);
    this.set(`mixer.${tr.outId}.filter`, 0.5, tr, true);
    let r = I.rate;
    if (tr.synced) {
      // 拍を合わせていた速度（直前 4 拍の平均）にテンポフェーダーを合わせてから SYNC を外す
      r = syncedRate(this.sync.bm[tr.inId], this.sync.bm[tr.outId], I.position(), O.position(), tr.k, O.rate || 1);
      r = 1 + Math.max(-1, Math.min(1, (r - 1) / I.tempoRange)) * I.tempoRange;      // テンポフェーダーの幅で頭打ち（高速書き出しと同じ）
      if (tr.rampTo) r = 1;          // テンポランプなら次の曲はもう原速（平均の計算がループの巻き戻りで揺れても戻し直さない）
      this.sync.disable(tr.inId, true);
      this.setRate(tr.inId, r);
    }
    this.sync.master = tr.inId;
    tr.phase = 'post';
    // 繋ぎの後は原速へ戻す（打合せ3・13。設定で OFF にできる）
    if (this.settings().tempo_return !== false && Math.abs(r - 1) > 0.002) {
      const bm = this.sync.bm[tr.inId];
      tr.ramp = { from: r, start: bm.pos(I.position()), beats: RETURN_BARS * (bm.meter || 4) };
    }
    this.onChange?.();
  }

  /**
   * パートごとに鳴らす準備（Phase 5 B2）: 2 曲の分離した音がそろうのを待ち（無ければ作り始める）、繋ぎの区間だけ読み込む。
   * waitSec まで待つ。通し再生では重なりが始まるまで待ち、間に合わなければその繋ぎはパートを分けない。
   */
  async prepareParts(tr, waitSec) {
    if (!tr.parts) return;
    const ids = [tr.itemOut.track.id, tr.itemIn.track.id], t0 = Date.now();
    let told = false, ok = false;
    for (;;) {
      if ((this.tr && this.tr !== tr) || tr.phase !== 'pre') return;      // 試聴では arm の前に呼ぶ（this.tr はまだ無い）
      const st = await Promise.all(ids.map(id => fetch(`/api/tracks/${id}/stems-audio`).then(r => r.json()).catch(() => ({ status: 'error' }))));
      if (st.every(s => s.status === 'done')) { ok = true; break; }
      if (st.some(s => s.status === 'error') || Date.now() - t0 > waitSec * 1000) break;
      if (!told) { this.flash?.('パートの音を用意しています（分離して保存。1 曲 20 秒ほど）…', 4000); told = true; }
      await sleep(1500);
    }
    if (!ok) { tr.parts = null; this.flash?.('パートの音を用意できなかったので、この繋ぎはパートを分けずに繋ぎます'); return; }
    const O = this.engine.decks[tr.outId], I = this.engine.decks[tr.inId];
    const bmO = this.sync.bm[tr.outId], bmI = this.sync.bm[tr.inId];
    const barO = bmO.beatLen(tr.ovStart) * (bmO.meter || 4), barI = bmI.beatLen(tr.inTime) * (bmI.meter || 4);
    const inEnd = bmI.time(bmI.pos(tr.inTime) + tr.k * tr.beats);
    const [a, b] = await Promise.all([
      O.loadStemSegment(tr.itemOut.track.id, tr.ovStart - 2 * barO, tr.ovEnd + barO),
      I.loadStemSegment(tr.itemIn.track.id, tr.inTime - 0.5, inEnd + 2 * barI),
    ]);
    if (!a || !b || (this.tr && this.tr !== tr) || tr.phase !== 'pre') {
      if (!this.tr || this.tr === tr) { tr.parts = null; O.clearStems(); I.clearStems(); }
      return;
    }
    I.setPartsMode(true, partGains(tr.join, 0, tr.beats).in);     // 次の曲は鳴り始めからパートの音（ドラムからなど）
    tr.partsReady = true;
  }

  /** デッキの速度をテンポフェーダーで設定する（フェーダーの幅で頭打ち）。 */
  setRate(id, rate) {
    const dk = this.engine.decks[id];
    const tf = Math.max(-1, Math.min(1, (rate - 1) / dk.tempoRange));
    actions.set(`deck.${id}.tempo`, 0.5 - tf / 2);
  }

  // ---- つなぎ目だけ試聴 ----
  /** items を渡すと、キュー以外の並び（おまかせの案）のつなぎ目も試聴できる。 */
  async previewJoin(i, items = this.getItems()) {
    if (this.busy) this.stop();
    const A = items[i], B = items[i + 1];
    if (!A || !B) return;
    if (A.track.analysis_status !== 'done' || B.track.analysis_status !== 'done') { this.flash?.('解析が終わってから試聴できます'); return; }
    this.mode = 'preview';
    this.onChange?.();
    try {
      for (const id of ['A', 'B']) if (this.engine.decks[id].playing) this.engine.decks[id].pause();
      const [dA, dB] = await Promise.all([this.trackData(A.track.id), this.trackData(B.track.id)]);
      await Promise.all([this.prepareDeck('A', A, dA), this.prepareDeck('B', B, dB)]);
      if (this.mode !== 'preview') return;
      this.saved = this.snapshot();
      const tr = this.plan('A', 'B', A, B, dA, dB, i);
      // 重なりの始まり（同期しないときは前の曲の終わり）の 8 小節前へ。位相を固定する（arm）より先にシークする。
      // 後からマスターをシークすると、SYNC の「マスターが飛んだらスレーブの位相をずらす」処理が働き、
      // 次の曲が範囲の頭からずれて始まる（P2-5 の試験で、次の曲が -102 秒から始まる不具合として見つけた）
      const from = tr.synced ? tr.startBar : tr.dOut.barTimes.findIndex(t => t >= tr.ovEnd - 0.05);
      this.engine.decks.A.seek(tr.dOut.barTimes[Math.max(0, from - 8)]);
      // パートの音は、繋ぎを始める（arm）前に用意する（arm の後は「前の曲が鳴っていない」と見なされて止まるため）
      await this.prepareParts(tr, 120);
      if (this.mode !== 'preview') return;
      this.arm(tr);
      if (tr.fellBack) this.flash?.(`繋ぎ方をエコーアウトに切り替えました: ${tr.reason}`);
      this.engine.decks.A.play();
      this.onChange?.();
    } catch (e) {
      this.flash?.(String(e.message ?? e));
      this.stop();
    }
  }

  // ---- ミックスを通して再生（§4.2） ----
  /** キューの start 番目の曲から、使う範囲どおりに順に流し、つなぎ目で自動的に繋ぐ。 */
  async playMix(start = 0) {
    if (this.busy) this.stop();
    const items = this.getItems(), it = items[start];
    if (!it) { this.flash?.('ミックスキューが空です'); return; }
    if (it.track.analysis_status !== 'done') { this.flash?.('最初の曲の解析が終わってから再生できます'); return; }
    this.mode = 'mix';
    this.mixIndex = start;
    this.saved = null;
    this.onChange?.();
    try {
      for (const id of ['A', 'B']) { const dk = this.engine.decks[id]; if (dk.playing) dk.pause(); this.sync.disable(id, false); }
      const d = await this.trackData(it.track.id);
      await this.prepareDeck('A', it, d);
      if (this.mode !== 'mix') return;
      // 最初の曲だけが聞こえる状態から始める（速度は原速）
      actions.set('mixer.crossfader', 0);
      for (const id of ['A', 'B']) {
        actions.set(`mixer.${id}.eq.low`, 0.5); actions.set(`mixer.${id}.filter`, 0.5); actions.set(`deck.${id}.tempo`, 0.5);
        this.engine.mixer.ch[id].setEcho(0);
      }
      const A = this.engine.decks.A;
      A.seek(this.useOf(it, d).t0);
      this.sync.master = 'A';
      A.play();
      this.onChange?.();
      await this.nextJoin('A', start);
    } catch (e) {
      this.flash?.(String(e.message ?? e));
      this.stop();
    }
  }

  /** デッキ outId で items[i] が鳴っている。次の曲をもう一方のデッキに読み込み、繋ぎを待機させる。次が無ければ最後の曲として扱う。 */
  async nextJoin(outId, i) {
    if (this.mode !== 'mix') return;
    const items = this.getItems(), itOut = items[i], itIn = items[i + 1];
    this.mixIndex = i;
    const dOut = await this.trackData(itOut.track.id);
    if (!itIn || itIn.track.analysis_status !== 'done') {
      if (itIn) this.flash?.(`「${itIn.track.title}」はまだ解析中なので、この曲でミックスを終えます`);
      this.tr = { phase: 'last', pair: i, outId, itemOut: itOut, t1: this.useOf(itOut, dOut).t1 };
      this.onChange?.();
      return;
    }
    const inId = outId === 'A' ? 'B' : 'A';
    const dIn = await this.trackData(itIn.track.id);
    await this.prepareDeck(inId, itIn, dIn);
    if (this.mode !== 'mix') return;
    const tr = this.plan(outId, inId, itOut, itIn, dOut, dIn, i);
    this.arm(tr);
    if (tr.fellBack) this.flash?.(`「${itOut.track.title} → ${itIn.track.title}」は繋ぎ方をエコーアウトに切り替えます: ${tr.reason}`, 5000);
    this.prepareParts(tr, 600).catch(e => console.error(e));     // 重なりが始まるまでに間に合えば使う
    this.onChange?.();
  }

  snapshot() {
    const names = ['mixer.crossfader', ...['A', 'B'].flatMap(id => [`mixer.${id}.eq.low`, `mixer.${id}.filter`])];
    return Object.fromEntries(names.map(n => [n, actions.get(n)?.get?.()]));
  }

  stop(message) {
    const tr = this.tr;
    this.tr = null;
    this.mode = null;
    for (const id of ['A', 'B']) {
      if (this.engine.decks[id].playing) this.engine.decks[id].pause();
      this.engine.mixer.ch[id].setEcho(0);
      this.engine.mixer.ch[id].resetJoinGain();
      this.engine.decks[id].clearStems();
    }
    if (tr?.inId) this.sync.disable(tr.inId, true);
    if (this.saved) { for (const [n, v] of Object.entries(this.saved)) if (v !== undefined) actions.set(n, v); this.saved = null; }
    if (message) this.flash?.(message);
    this.onChange?.();
  }
}

function label(name) {
  if (name === 'mixer.crossfader') return 'クロスフェーダー';
  const m = name.match(/^mixer\.([AB])\.(eq\.low|filter)$/);
  return m ? `デッキ${m[1]}の${m[2] === 'filter' ? 'フィルター' : 'LOW'}` : name;
}
