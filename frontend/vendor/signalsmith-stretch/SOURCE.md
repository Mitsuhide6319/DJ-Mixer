# Signalsmith Stretch (Web release) v1.3.2

- 取得元: https://cdn.jsdelivr.net/npm/signalsmith-stretch@1.3.2/ （npm パッケージ `signalsmith-stretch`）
- 作者: Geraint Luff / Signalsmith Audio
- ライセンス: MIT。本文は同じフォルダの `LICENSE.txt`（npm パッケージに本文が無かったため、本家リポジトリ https://github.com/Signalsmith-Audio/signalsmith-stretch の LICENSE.txt を 2026-10-25 に取得して同梱）
- 取得日: 2026-09-28
- 同梱ファイル: `SignalsmithStretch.mjs`（ES Module、WASM 埋め込み）、`README.md`、`package.json`

## ローカルの修正（2026-09-28、P1-4）

`SignalsmithStretch.mjs` の AudioWorklet 内、入力バッファから必要区間をコピーするループ（`process()` 内）に不具合があった。

- 元のコード: 部分コピーの後に `audioSamples += count;`（コピーした量だけ進める。`inputSamples` は進めない）
- 症状: `addBuffers` で複数のバッファを入れた状態で、あるバッファの先頭から約 0.12 秒（ブロック長）以上奥を再生すると、
  次のバッファの開始位置を誤って計算し、満杯の一時バッファにさらに書き込もうとして
  `Uncaught RangeError: offset is out of bounds` が AudioWorklet 内で発生し、ノードが無音のまま停止する。
  バッファが 1 つだけのとき・最後のバッファを読んでいるときは起きない。
- 修正: `inputSamples += count; audioSamples = bufferEnd;` に変更（`// [dj_mixer patch]` の箇所）
- 確認: 10 秒チャンクを 72 個（720 秒分）入れて途中位置を再生しても停止しないこと（report.md の P1-4 記録）
- ライブラリを更新するときは、上流で直っているか確認し、直っていなければこの修正を当て直すこと

## ローカルの修正 2（2026-09-29、P2-5）

`schedule(obj, adjustPrevious)`（AudioWorklet 側）で、「この予約より後の予約を消す」判定の基準時刻が、`obj.output` ではなく
`currentTime`（今）になっていた（`outputTime` という別名のキーだけを見ていた）。

- 症状: 未来の時刻（今＋100ms）に 20ms ごとに予約を重ねると、そのたびに直前の予約（まだ未来）まで消える。
  すでに再生中だったデッキでは過去の区間が残るので気づかないが、**「SYNC 状態で再生を始める」と、再生開始の予約が毎回消されて
  停止状態の区間しか残らず、無音のまま位置だけが進む**（P2-5 の「つなぎ目だけ試聴」で、次の曲が鳴らない不具合として発見）
- 修正: `let outputTime = ('outputTime' in objIn) ? objIn.outputTime : ('output' in objIn ? objIn.output : currentTime);`（`// [dj_mixer patch 2]`）
- 効果: 予約は指定した出力時刻以降だけが置き換わる（README の説明どおりの動作）。20ms ごとの予約はすべて残り、adjustPrevious で
  隣どうしが連続につながる。デッキ側の位置の計算（deck.js のアンカー履歴）とも一致するようになる
- 同じ関数の後半（報告用の位置を求めるために予約の列の先頭を捨てる処理）も `outputTime` を使っていたので、`currentTime` 基準の
  `reportTime` に分けた。基準が未来の時刻になると、今鳴っている区間まで捨ててしまうため（元コードでは outputTime が常に今だったので同じ動作）
