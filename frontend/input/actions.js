// アクション（design_phase1.md §6.1）。すべての操作を名前付きアクションとして登録し、
// マウス・キーボード・MIDI（P1-9）はアクションを呼ぶだけにする。
//
// 種別: trigger（押す/離す）、toggle（on/off。状態を返す）、absolute（0..1）、relative（±ステップ）
const registry = new Map();
const stateListeners = new Set();
let contextHandler = null;

/** 操作子を右クリックしたときの処理を登録する。fn(actionName, element, event)。 */
export function setContextHandler(fn) { contextHandler = fn; }

export function register(name, def) { registry.set(name, def); }
export const get = name => registry.get(name);
export const names = () => [...registry.keys()];

/** info: {shift} など、押したときの修飾キー（ホットキューの削除などに使う）。 */
export function trigger(name, down = true, info = {}) {
  const a = registry.get(name);
  if (!a) return;
  if (a.type === 'trigger') (down ? a.down : a.up)?.(info);
  else if (a.type === 'toggle' && down) a.toggle(info);
  notify(name);
}
export function set(name, v) { const a = registry.get(name); if (a?.set) { a.set(v); notify(name); } }
export function step(name, d) { const a = registry.get(name); if (a?.step) { a.step(d); notify(name); } }
export function state(name) { return registry.get(name)?.state?.(); }

/** トグルの状態が変わったことを知らせる（ボタンの点灯・MIDI の LED 用）。 */
export function notify(name) { stateListeners.forEach(fn => fn(name)); }
export const onState = fn => stateListeners.add(fn);

/** 画面の [data-act] 要素（ボタン）とアクションをつなぐ。フェーダー・ノブは各ビューが set() を呼ぶ。 */
export function bindButtons(root = document) {
  root.addEventListener('pointerdown', e => {
    const el = e.target.closest('.btn[data-act]');
    if (!el || !registry.has(el.dataset.act)) return;
    const name = el.dataset.act;
    if (e.button === 2) return;                 // 右クリックは contextmenu 側で扱う
    trigger(name, true, { shift: e.shiftKey });
    if (registry.get(name).type === 'trigger') {
      addEventListener('pointerup', () => trigger(name, false), { once: true });
    }
  });
  root.addEventListener('contextmenu', e => {
    const el = e.target.closest('[data-act]');
    if (!el || !contextHandler) return;
    e.preventDefault();
    contextHandler(el.dataset.act, el, e);     // 右クリックのメニュー（ホットキューの削除・MIDI 割り当て）はアプリ側で出す
  });
  const refresh = () => {
    for (const el of root.querySelectorAll('.btn[data-act]')) {
      const a = registry.get(el.dataset.act);
      if (a?.state) el.classList.toggle('on', !!a.state());
    }
  };
  onState(refresh);
  refresh();
  return refresh;
}
