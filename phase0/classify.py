"""Phase 0 の解析結果を A/B/C に分類し、確認用 HTML(work/phase0/index.html) を作る。"""
import html
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "work" / "phase0"


def classify(r: dict) -> tuple[str, list[str]]:
    t, b = r["tempo"], r["bars"]
    reasons = []
    local = t["local16_grid_rms_ms"] or 999
    if r["beat_conf_mean"] < 0.9 or local > 30 or t["unstable_ratio"] > 0.4:
        return "C", ["拍の検出が不安定"]
    if t["unstable_ratio"] > 0.05:
        reasons.append(f"倍/半テンポの取り違え区間 {t['unstable_ratio']:.0%}")
    if b["dominant_ratio"] < 0.85:
        reasons.append(f"小節頭の揺らぎ（{b['dominant_meter']}拍子が{b['dominant_ratio']:.0%}）")
    if abs(t["drift_pct"]) > 5:
        reasons.append(f"テンポが曲中で {t['drift_pct']:+.0f}% 変化")
    if r["no_beat_total_s"] > 15:
        reasons.append(f"拍なし区間 {r['no_beat_total_s']}秒")
    return ("A" if not reasons else "B"), reasons


def main():
    rows = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    out = []
    for r in rows:
        cls, reasons = classify(r)
        r["class"], r["reasons"] = cls, reasons
        out.append(r)
    (OUT / "classified.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in out:
        t = r["tempo"]
        print(f"{r['class']} {r['id']:16s} {r['genre']:6s} {t['bpm_median']:6.1f}BPM {t['bpm_p5']:.0f}-{t['bpm_p95']:.0f} "
              f"meter{r['bars']['dominant_meter']} lufs{r['lufs']:6.1f} {r['key']['camelot']:>3s} | {' / '.join(r['reasons'])}")
    cnt = {c: sum(r["class"] == c for r in out) for c in "ABC"}
    print(cnt)

    cards = []
    for r in out:
        t = r["tempo"]
        cards.append(f"""<section class="card c{r['class']}">
<h2><span class="badge">{r['class']}</span> {html.escape(r['id'])} <small>{html.escape(r['genre'])} — {html.escape(r['note'])}</small></h2>
<p class="path">{html.escape(r['path'])}</p>
<p>BPM {t['bpm_median']}（5〜95%: {t['bpm_p5']}〜{t['bpm_p95']}） / 変化 {t['drift_pct']:+}% / 拍子 {r['bars']['dominant_meter']}（{r['bars']['dominant_ratio']:.0%}） / LUFS {r['lufs']} / キー {r['key']['key']}（{r['key']['camelot']}） 冒頭 {r['key_head30']['camelot']} 末尾 {r['key_tail30']['camelot']}</p>
<p class="reasons">{html.escape(' / '.join(r['reasons']) or '問題なし')}</p>
<img src="{r['id']}/tempo.png" loading="lazy">
<audio controls preload="none" src="{r['id']}/click.mp3"></audio>
</section>""")
    stretch = ""
    sj = OUT / "stretch" / "stretch_result.json"
    if sj.exists():
        st = json.loads(sj.read_text(encoding="utf-8"))
        fo, fx = st["follow_bluetrain"], st["fixed_ratio_bluetrain"]
        items = "".join(
            f'<li>{k}（倍率 {v["factor"]}）<audio controls preload="none" src="stretch/{k}.mp3"></audio></li>'
            for k, v in st.items() if k.startswith("const_"))
        stretch = f"""<section class="card"><h2>タイムストレッチ試験（Signalsmith Stretch）</h2>
<p>一定倍率の音質（1.0 が原音。0.917≒-8%、0.834≒-17%、0.945≒-5.5%、0.88≒-12%）:</p><ul>{items}</ul>
<p><b>テンポ追従</b>: Blue Train（入力 {fo['input_bpm_range'][0]}〜{fo['input_bpm_range'][1]} BPM で揺れる）を 140 BPM の固定クリックに追従させたもの。
50ms 以上ずれた拍 {fo['offbeat_ratio_50ms']:.0%}、揺れ p95 {fo['jitter_p95_ms']}ms。</p>
<audio controls preload="none" src="stretch/follow_bluetrain_140_click.mp3"></audio>
<p>比較: 一定倍率で伸縮しただけ（一般的なDJソフトの固定グリッド相当）。50ms 以上ずれた拍 {fx['offbeat_ratio_50ms']:.0%}。途中からクリックと演奏がずれていくのが聴こえるはず。</p>
<audio controls preload="none" src="stretch/fixed_bluetrain_140_click.mp3"></audio></section>"""

    page = f"""<!doctype html><html lang="ja"><meta charset="utf-8"><title>Phase 0 解析確認</title>
<style>
:root{{--bg:#fafafa;--fg:#222;--card:#fff;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e8e8e8;--card:#1f2228;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font-family:"Yu Gothic UI",sans-serif;max-width:1100px;margin:0 auto;padding:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-left:6px solid #888;border-radius:6px;padding:12px 16px;margin:14px 0}}
.cA{{border-left-color:#2e9e5b}}.cB{{border-left-color:#e0a000}}.cC{{border-left-color:#d64545}}
.badge{{display:inline-block;min-width:1.6em;text-align:center;border-radius:4px;background:#444;color:#fff}}
.cA .badge{{background:#2e9e5b}}.cB .badge{{background:#e0a000}}.cC .badge{{background:#d64545}}
h2{{font-size:1.05em;margin:0 0 4px}} small{{font-weight:normal;opacity:.7}} .path{{font-size:.8em;opacity:.6;margin:0}}
img{{width:100%;background:#fff;border-radius:4px}} audio{{width:100%;margin-top:6px}} .reasons{{font-weight:bold}}
</style>
<h1>Phase 0 解析確認（{len(out)}曲: A={cnt['A']} B={cnt['B']} C={cnt['C']}）</h1>
<p>A=そのまま拍合わせに使える / B=自動補正・手修正で使える / C=拍合わせ不可（フェード系の繋ぎのみ）。<br>
音声: 高い「ピッ」=小節頭、低い「ポッ」=拍。音量は -16 LUFS 付近に揃えてあります。グラフの赤帯=拍なし区間、下段=小節ごとの拍数。</p>
{stretch}
{''.join(cards)}
</html>"""
    (OUT / "index.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
