"""Phase 4 モックアップ（D1・D2・A・B）を単体 HTML にする（ダブルクリックで開ける）。Phase 2 の共通部品（波形の描画など）を使う。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
css = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "frontend" / "theme.css", ROOT / "mockup" / "phase2" / "p2_common.css",
                                                      ROOT / "mockup" / "phase3" / "p3_common.css", HERE / "p4_common.css"))
data = (ROOT / "work" / "phase4" / "mock4_data.json").read_text(encoding="utf-8")
common = (ROOT / "mockup" / "phase2" / "common.js").read_text(encoding="utf-8")
for key in sys.argv[1:] or ["D1", "D2", "A", "B"]:
    src = HERE / f"p4_{key}.src.html"
    if not src.exists():
        continue
    s = src.read_text(encoding="utf-8")
    s = s.replace("<!--CSS-->", f"<style>{css}</style>").replace("<!--DATA-->", f"<script>window.MOCK2={data};</script>")
    s = s.replace("<!--COMMON-->", f"<script>{common}</script>")
    out = HERE / f"ui_p4_{key}.html"
    out.write_text(s, encoding="utf-8")
    print(out.name, len(s.encode()) // 1024, "KB")
