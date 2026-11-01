// ミキサー（design_phase1.md §5.1、打合せ7・9）。行ごとに「A のノブ｜名前｜B のノブ」。
// ノブの機能名は 16px 太字（打合せ9）。値の変化は onChange(action, value) で通知する。
import { makeKnob, makeFader } from './controls.js';

const ROWS = [
  ['gain', 'GAIN', true],
  ['eq.high', 'HI', false],
  ['eq.mid', 'MID', false],
  ['eq.low', 'LOW', false],
  ['filter', 'FILTER', false],
];

export function renderMixer(root, { colors, onChange = () => {} }) {
  root.innerHTML = `<h4 class="a">CH A</h4><div></div><h4 class="b">CH B</h4>`;
  const knobs = {};
  for (const [key, name, tinted] of ROWS) {
    for (const deck of ['A', 'B']) {
      const act = `mixer.${deck}.${key}`;
      const k = makeKnob({ color: tinted ? colors[deck] : '#9aa3ad', onChange: v => onChange(act, v) });
      k.el.dataset.act = act;
      knobs[act] = k;
      if (deck === 'A') {
        root.append(k.el);
        const label = document.createElement('div');
        label.className = 'mname';
        label.textContent = name;
        root.append(label);
      } else {
        root.append(k.el);
      }
    }
  }
  const tail = document.createElement('div');
  tail.style.display = 'contents';
  tail.innerHTML = `
    <div class="fadrow">
      <div class="vfader chfader" data-act="mixer.A.fader"><div class="cap"></div></div>
      <div class="meters"><div class="vu"><i data-vu="A" style="height:0%"></i></div><div class="vu"><i data-vu="B" style="height:0%"></i></div></div>
      <div class="vfader chfader" data-act="mixer.B.fader"><div class="cap"></div></div>
    </div>
    <div class="xf"><div class="hfader" data-act="mixer.crossfader"><div class="hcap"></div></div>
      <div class="seg" data-role="xfcurve"><div class="on" data-curve="smooth">なめらか</div><div data-curve="linear">リニア</div><div data-curve="cut">カット</div></div></div>`;
  root.append(tail);

  const faders = {};
  for (const el of root.querySelectorAll('.vfader, .hfader')) {
    const act = el.dataset.act;
    faders[act] = makeFader({ el, vertical: el.classList.contains('vfader'), value: act === 'mixer.crossfader' ? 0.5 : 1,
                              onChange: v => onChange(act, v) });
  }
  root.querySelector('[data-role=xfcurve]').addEventListener('click', e => {
    const c = e.target.closest('[data-curve]');
    if (!c) return;
    root.querySelectorAll('[data-curve]').forEach(x => x.classList.toggle('on', x === c));
    onChange('mixer.crossfader.curve', c.dataset.curve);
  });
  const meters = { A: root.querySelector('[data-vu=A]'), B: root.querySelector('[data-vu=B]') };
  return { knobs, faders, setMeter: (deck, v) => { meters[deck].style.height = `${Math.round(v * 100)}%`; } };
}
