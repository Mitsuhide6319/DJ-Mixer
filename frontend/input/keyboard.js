// キーボードの既定割り当て（design_phase1.md §6.3）。キーはアクションを呼ぶだけ。
import * as actions from './actions.js';

// e.code（物理キー）で割り当てる。日本語配列・IME の状態に左右されないように
export const DEFAULT_KEYS = {
  KeyZ: 'deck.A.play', KeyX: 'deck.A.cue', KeyC: 'deck.A.sync', KeyA: 'deck.A.nudge.minus', KeyS: 'deck.A.nudge.plus',
  Digit1: 'deck.A.hotcue.1', Digit2: 'deck.A.hotcue.2', Digit3: 'deck.A.hotcue.3', Digit4: 'deck.A.hotcue.4',
  KeyM: 'deck.B.play', Comma: 'deck.B.cue', Period: 'deck.B.sync', KeyK: 'deck.B.nudge.minus', KeyL: 'deck.B.nudge.plus',
  Digit7: 'deck.B.hotcue.1', Digit8: 'deck.B.hotcue.2', Digit9: 'deck.B.hotcue.3', Digit0: 'deck.B.hotcue.4',
  KeyQ: 'ui.queue.toggle',
};

export function initKeyboard(map = DEFAULT_KEYS) {
  const held = new Set();
  addEventListener('keydown', e => {
    if (e.target.closest?.('input, textarea, select') || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') {
      // クロスフェーダー（Shift で大きく）
      const a = actions.get('mixer.crossfader');
      if (a?.get) actions.set('mixer.crossfader', Math.max(0, Math.min(1, a.get() + (e.code === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 0.2 : 0.05))));
      e.preventDefault();
      return;
    }
    const name = map[e.code];
    if (!name) return;
    e.preventDefault();
    if (held.has(e.code)) return;             // キーリピートは無視
    held.add(e.code);
    actions.trigger(name, true, { shift: e.shiftKey });
  });
  addEventListener('keyup', e => {
    if (!held.delete(e.code)) return;
    const name = map[e.code];
    if (name && actions.get(name)?.type === 'trigger') actions.trigger(name, false);
  });
  addEventListener('blur', () => {             // ウィンドウを離れたら押しっぱなしを解除
    for (const c of held) { const n = map[c]; if (n && actions.get(n)?.type === 'trigger') actions.trigger(n, false); }
    held.clear();
  });
}
