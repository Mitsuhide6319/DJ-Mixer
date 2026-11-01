// デッキ画面（design_phase1.md §5.1、打合せ8）。
// 中央（ミキサー）を軸に線対称: A は操作を右寄せ・テンポ左端、B は操作を左寄せ・テンポ右端。
// 反転するのは「まとまり」の位置だけで、対になるボタン（◀1拍/1拍▶、×½/×2、拍/小節）の中と、
// ホットキュー・ループの数字は読み順のまま。各ボタンは data-act にアクション名を持つ（P1-9 で接続）。
import { makeFader } from './controls.js';

const LOOP_SIZES = ['1/4', '1/2', '1', '2', '4', '8', '16'];

export function renderDeck(root, deck) {
  const isA = deck === 'A';
  const m = items => (isA ? items : [...items].reverse()).join('');
  const b = (act, label, cls = '') => `<div class="btn ${cls}" data-act="deck.${deck}.${act}">${label}</div>`;

  const main = `<div class="dmain">
    <div class="row">${m([b('play', '▶︎ ▮▮', 'big play'), b('cue', 'CUE', 'big')])}</div>
    <div class="row">${m([b('sync', 'SYNC', 'w3'), b('master', 'MASTER', 'w3'), b('keylock', 'KEYLOCK', 'w3')])}</div>
    <div class="row">${m([b('beatjump.back', '◀ 1拍', 'w4') + b('beatjump.fwd', '1拍 ▶', 'w4'),
                          b('tempo.half', '×½', 'w4') + b('tempo.double', '×2', 'w4')])}</div>
    <div class="lbl">HOT CUE</div>
    <div class="row">${[1, 2, 3, 4].map(i => b(`hotcue.${i}`, i, 'pad')).join('')}</div>
    <div class="lbl">LOOP</div>
    <div class="row">${LOOP_SIZES.map(x => b(`loop.${x}`, x, 'loopbtn')).join('')}</div>
    <div class="row">${m([b('loop.unit.beat', '拍', 'w5') + b('loop.unit.bar', '小節', 'w5'),
                          b('grid.downbeat', 'ここを1拍目に', 'w6') + b('grid.meter', '拍子: 自動', 'w6')])}</div>
    <div class="phase" data-role="status"><span class="empty">曲が読み込まれていません</span></div>
  </div>`;
  const tempo = `<div class="tempo"><div class="lbl">TEMPO</div>
    <div class="vfader" data-act="deck.${deck}.tempo"><div class="cap"></div></div>
    <div class="val" data-role="range">±8%</div><div class="val" data-role="tempoval">+0.0%</div></div>`;
  root.innerHTML = isA ? tempo + main : main + tempo;

  const tempoFader = makeFader({ el: root.querySelector('.vfader'), vertical: true, value: 0.5 });
  return { root, tempoFader };
}
