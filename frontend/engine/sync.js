// SYNC（design_phase1.md §4.3）。スレーブデッキを、マスターの「拍位置」に追従させる。
//
//   スレーブの目標入力位置 = timeAt_s( k × pos_m(マスターの入力位置) + φ )
//   k: 倍/半テンポ係数（1, 2, 0.5）  φ: 位相オフセット（拍単位）
//
// 20ms ごと（AudioWorklet の刻み。画面が隠れてもタイマー制限で止まらない）に、100ms 先の出力時刻について
// 目標位置と速度を計算し、schedule(…, adjustPrevious=true) で予約する。テンポが揺れる曲どうしでも、
// 拍ごとに速度が追従する（Phase 0 で検証した方式）。
//
// SYNC を押したときの位相合わせは、最寄りの拍へ一気に飛ばさず、速度を少し変えて数秒かけて寄せる
// （DJ がピッチベンドで合わせるのと同じ）。◀1拍/1拍▶ だけは即座にずらす。
import { BeatMap } from './beatmap.js';

const LOOK = 0.1;              // 予約する出力時刻の先読み
const DT = 0.1;                // 速度を求める差分の幅
const PHASE_STEP = 0.004;      // 位相を寄せる速さ（拍/刻み。20ms 刻みで 0.2 拍/秒）
const RATE_MIN = 0.7, RATE_MAX = 1.3;
const PREFERRED = 0.16;        // これ以内の速度変化になる k を優先

const TICKER = `
class Ticker extends AudioWorkletProcessor {
  constructor() { super(); this.n = 0; }
  process() { this.n += 128; if (this.n >= ${Math.round(44100 * 0.02)}) { this.n = 0; this.port.postMessage(0); } return true; }
}
registerProcessor('djmix-ticker', Ticker);`;

export class SyncController {
  constructor(engine) {
    this.engine = engine;
    this.master = null;                  // 'A' | 'B' | null（null なら自動）
    this.st = { A: this.fresh(), B: this.fresh() };
    this.barMode = false;                // true: 小節の頭をそろえる（小節頭の信頼度が高いときだけ）
    this.listeners = new Set();
    this.tickListeners = new Set();       // 繋ぎの実行（runner.js）も同じ刻みで動く
    this.bm = { A: null, B: null };
  }

  fresh() { return { on: false, k: 1, phi: 0, phiTarget: 0, hold: null, nudge: 0, fixed: false }; }
  on(fn) { this.listeners.add(fn); }
  emit() { this.listeners.forEach(fn => fn()); }
  other(id) { return id === 'A' ? 'B' : 'A'; }
  deck(id) { return this.engine.decks[id]; }

  async init() {
    const ctx = this.engine.ctx;
    const url = URL.createObjectURL(new Blob([TICKER], { type: 'text/javascript' }));
    await ctx.audioWorklet.addModule(url);
    const node = new AudioWorkletNode(ctx, 'djmix-ticker', { numberOfOutputs: 1 });
    const mute = ctx.createGain();
    mute.gain.value = 0;
    node.connect(mute).connect(ctx.destination);      // 出力をつながないと process() が呼ばれない
    node.port.onmessage = () => this.tick();
    for (const id of ['A', 'B']) {
      const d = this.deck(id);
      d.syncHook = t => this.startInput(id, t);
      d.on(what => {
        if (what === 'load') { this.bm[id] = null; this.disable(id, false); }
        if (what === 'analysis') this.bm[id] = new BeatMap(d.analysis);
        if (what === 'state') this.autoMaster();
        if (what === 'seek') this.onSeek(id, d.lastSeek);
      });
    }
    return this;
  }

  /** いまのマスター。明示されていなければ、鳴っているデッキ（両方なら先に鳴っていた方）。 */
  masterId() {
    if (this.master) return this.master;
    const a = this.deck('A'), b = this.deck('B');
    if (a.playing && !b.playing) return 'A';
    if (b.playing && !a.playing) return 'B';
    if (this.st.A.on && !this.st.B.on) return 'B';
    if (this.st.B.on && !this.st.A.on) return 'A';
    return 'A';
  }

  /** マスターが止まり、スレーブだけが鳴っている → 鳴っている側をマスターに引き継ぐ。 */
  autoMaster() {
    const m = this.masterId(), s = this.other(m);
    if (!this.deck(m).playing && this.deck(s).playing && this.st[s].on) {
      this.disable(s, true);
      this.master = s;
    }
    this.emit();
  }

  setMaster(id) {
    this.master = id;
    if (this.st[id].on) this.disable(id, true);
    this.emit();
  }

  /** SYNC を有効にする。戻り値: {ok, reason}。 */
  enable(id) {
    const m = this.other(id);
    if (!this.bm[id] || !this.bm[m]) return { ok: false, reason: '解析が終わっていない曲があります' };
    if (!this.bm[id].usable || !this.bm[m].usable) return { ok: false, reason: '拍を検出できていない曲です' };
    if (this.master === id) this.master = m;
    const S = this.deck(id), M = this.deck(m);
    const t = this.engine.now + LOOK;
    const k = this.chooseK(id, m, t);
    if (!k) return { ok: false, reason: 'テンポの差が大きすぎます（±30% 以内になる倍/半がありません）' };
    const st = this.st[id];
    st.on = true;
    st.k = k;
    const pm = this.bm[m].pos(M.position(t)), ps = this.bm[id].pos(S.position(t));
    st.phi = ps - k * pm;                       // いまのずれをそのまま起点にし、目標へ少しずつ寄せる
    st.phiTarget = this.roundPhase(id, m, st.phi, pm, k);
    st.hold = null;
    this.emit();
    return { ok: true };
  }

  /** SYNC を切る。keepRate=true なら今の速度をテンポフェーダーの値にして鳴らし続ける。 */
  disable(id, keepRate = true) {
    const st = this.st[id];
    if (!st.on) return;
    st.on = false;
    st.fixed = false;
    const d = this.deck(id);
    if (keepRate) {
      d.tempoFader = Math.max(-1, Math.min(1, (d.rate - 1) / d.tempoRange));
      d.applyRate(d.rate);
    }
    this.emit();
  }

  /** 1 拍ずらす（◀1拍 / 1拍▶）。即座に移動する。 */
  shiftBeat(id, d) {
    const st = this.st[id];
    st.phi += d;
    st.phiTarget += d;
  }

  /** ×½ / ×2。 */
  setK(id, k) {
    const st = this.st[id];
    if (!st.on) return;
    const m = this.other(id), t = this.engine.now + LOOK;
    const pm = this.bm[m].pos(this.deck(m).position(t)), ps = this.bm[id].pos(this.deck(id).position(t));
    st.k = k;
    st.phi = ps - k * pm;
    st.phiTarget = this.roundPhase(id, m, st.phi, pm, k);
    this.emit();
  }

  /** シーク・ホットキューで飛んだとき、SYNC の位相を保つ。
   *  スレーブが飛んだ → 位相オフセットを飛んだ拍数だけずらす（クオンタイズして飛べば拍はそろったまま）。
   *  マスターが飛んだ → スレーブはそのまま鳴らし続けるよう、スレーブの位相オフセットを逆向きにずらす。 */
  onSeek(id, s) {
    if (!s || !this.bm[id]) return;
    const d = this.bm[id].pos(s.to) - this.bm[id].pos(s.from);
    const st = this.st[id];
    if (st.on) {
      st.phi += d;
      st.phiTarget = Math.round(st.phiTarget + d);
    }
    const other = this.other(id), so = this.st[other];
    // 繋ぎのために位相を固定しているスレーブは、マスターのシークで動かさない（位相は runner.js が決める）
    if (so.on && !so.fixed && this.masterId() === id) {
      so.phi -= so.k * d;
      so.phiTarget = Math.round(so.phiTarget - so.k * d);   // クオンタイズして飛べば整数のまま。そうでなければ最寄りの拍へ寄せる
      so.lastPm = undefined;
    }
  }

  /** 必要な速度が 1 に近くなる k。±16% 以内を優先、±30% を超えるなら null。 */
  chooseK(id, m, t) {
    const S = this.deck(id), M = this.deck(m);
    const bpmS = 60 / this.bm[id].beatLen(S.position(t));
    const bpmM = 60 / this.bm[m].beatLen(M.position(t)) * M.rate;
    let best = null;
    for (const k of [1, 2, 0.5]) {
      const rate = k * bpmM / bpmS;
      const dev = Math.abs(Math.log(rate));
      if (!best || dev < best.dev) best = { k, dev, rate };
    }
    if (best.rate < RATE_MIN || best.rate > RATE_MAX) return null;
    return best.k;
  }

  /** 位相の目標: 最寄りの拍（小節モードなら小節の頭がそろう拍）。 */
  roundPhase(id, m, phi, pm, k) {
    let target = Math.round(phi);
    if (this.barMode && k === 1) {
      const bmS = this.bm[id], bmM = this.bm[m];
      const inM = bmM.beatInBar(pm).beat;
      let best = null;
      for (let d = -4; d <= 4; d++) {
        const inS = bmS.beatInBar(pm + target + d).beat;
        const diff = Math.abs(inS - inM);
        if (!best || diff < best.diff) best = { d, diff };
      }
      target += best.d;
    }
    return target;
  }

  /** スレーブ自身のループ中は、目標の拍位置をループの中で折り返す。 */
  wrapLoop(id, p) {
    const L = this.deck(id).loop;
    if (!L?.active) return p;
    const bm = this.bm[id], a = bm.pos(L.start), b = bm.pos(L.end), len = b - a;
    return p >= b ? a + ((p - a) % len) : p;
  }

  /** 出力時刻 T でのスレーブの目標入力位置。拍なし区間などで追従できなければ hold に理由を入れて返す。 */
  target(id, T) {
    const m = this.other(id), st = this.st[id], M = this.deck(m);
    const inM = M.position(T);
    if (this.bm[m].inNoBeat(inM)) return { hold: 'マスター側が拍なし区間' };
    const praw = st.k * this.bm[m].pos(inM) + st.phi;
    const p = this.wrapLoop(id, praw);
    const inS = this.bm[id].time(p);
    if (this.bm[id].inNoBeat(inS)) return { hold: 'この曲が拍なし区間' };
    // 速度は折り返し前の拍位置の差分から求める（ループの境目で速度が乱れないように）
    const praw2 = st.k * this.bm[m].pos(M.position(T + DT)) + st.phi;
    const rate = (this.bm[id].time(praw2) - this.bm[id].time(praw)) / DT;
    if (rate < RATE_MIN || rate > RATE_MAX) return { hold: `必要な速度 ${(rate * 100 - 100).toFixed(0)}% が範囲外` };
    return { inS, rate };
  }

  /** 同期中のスレーブを再生し始めるとき、拍がそろった位置から始める（Deck.play から呼ばれる）。 */
  startInput(id, T) {
    const st = this.st[id], m = this.other(id);
    if (!st.on || !this.deck(m).playing || !this.bm[id] || !this.bm[m]) return null;
    const S = this.deck(id);
    const pm = this.bm[m].pos(this.deck(m).position(T)), ps = this.bm[id].pos(S.position(T));
    if (!st.fixed) {
      st.phi = st.phiTarget = Math.round(ps - st.k * pm);     // 止まっていた位置の最寄りの拍から、すぐにそろえて始める
      if (this.barMode) st.phi = st.phiTarget = this.roundPhase(id, m, st.phi, pm, st.k);
    }
    const r = this.target(id, T);
    return r.inS !== undefined ? { inT: r.inS, rate: r.rate } : null;
  }

  /** 繋ぎ用: 位相を丸めずに固定して SYNC する（design_phase2.md §4.1）。次の曲の使う範囲の頭（小節頭）が、
   *  前の曲の重なりの始まり（小節頭）にぴったり重なる phi を渡す。 */
  armFixed(id, k, phi) {
    const m = this.other(id);
    this.master = m;
    Object.assign(this.st[id], { on: true, k, phi, phiTarget: phi, hold: null, nudge: 0, fixed: true });
    this.emit();
  }

  onTick(fn) { this.tickListeners.add(fn); return () => this.tickListeners.delete(fn); }

  tick() {
    // 曲の終わりの検出もここで行う（描画ループは画面が隠れると止まるが、この刻みは音声スレッドから来るので止まらない）
    for (const id of ['A', 'B']) this.deck(id).tick();
    this.tickListeners.forEach(fn => fn());
    const T = this.engine.now + LOOK;
    for (const id of ['A', 'B']) {
      const st = this.st[id], S = this.deck(id), M = this.deck(this.other(id));
      if (!st.on || !S.playing || !M.playing || !this.bm[id] || !this.bm[this.other(id)]) continue;
      // ナッジ（押している間だけ位相を少しずつずらす。0.5 拍/秒）
      if (st.nudge) { st.phi += st.nudge * 0.01; st.phiTarget += st.nudge * 0.01; }
      // 位相を目標へ少しずつ寄せる
      const d = st.phiTarget - st.phi;
      st.phi += Math.max(-PHASE_STEP, Math.min(PHASE_STEP, d));
      // マスターがループで巻き戻ったら、スレーブは巻き戻さずに鳴らし続ける（位相オフセットをループの拍数だけ進める）
      const mid = this.other(id), pm = this.bm[mid].pos(M.position(T));
      if (st.lastPm !== undefined && M.loop?.active && pm < st.lastPm - 0.5) {
        const shift = st.k * (st.lastPm - pm);
        const L = Math.round(shift * 4) / 4;          // ループの拍数（1/4 拍単位）
        st.phi += L;
        st.phiTarget += L;
      }
      st.lastPm = pm;
      const r = this.target(id, T);
      if (r.hold) {                       // 追従できない間は今の速度のまま鳴らし続ける
        if (st.hold !== r.hold) { st.hold = r.hold; this.emit(); }
        continue;
      }
      if (st.hold) { st.hold = null; this.emit(); }
      // スレーブ自身のループで折り返した予約は、直前区間の速度調整をしない（負の速度になるため）
      const wrapped = r.inS < S.anchor.inT - 0.02 && !!S.loop?.active;
      S.scheduleAt(T, r.inS, r.rate, !wrapped);
    }
  }

  /** 位相のずれ（ms）。内部の再生位置から求めるので、位相を寄せている途中の残りを表す。
   *  ノードが報告する入力位置は、schedule() 直後の報告（遅延補正なし）と処理中の報告（入出力の遅延込み、約 120ms）が
   *  混ざるため、表示には使わない（P1-6 で確認）。実際に鳴った音でのずれは E2E 試験で測る。 */
  phaseErrorMs(id) {
    const st = this.st[id], m = this.other(id);
    if (!st.on || !this.bm[id] || !this.bm[m] || this.deck(id).loop?.active) return null;   // ループ中は比べない
    const now = this.engine.now, S = this.deck(id), M = this.deck(m);
    const inS = S.position(now);
    const err = this.bm[id].pos(inS) - (st.k * this.bm[m].pos(M.position(now)) + st.phiTarget);
    return err * this.bm[id].beatLen(inS) / S.rate * 1000;
  }
}
