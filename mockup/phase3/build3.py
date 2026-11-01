"""Phase 3 モックアップ（案X・Y・Z）を、本番のテーマ・共通部品・試作の実データを埋め込んだ単体 HTML にする（ダブルクリックで開ける）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
css = "".join((p).read_text(encoding="utf-8") for p in (ROOT / "frontend" / "theme.css", ROOT / "mockup" / "phase2" / "p2_common.css", HERE / "p3_common.css"))
data = (ROOT / "work" / "phase3" / "mock3_data.json").read_text(encoding="utf-8")
common = (HERE / "common3.js").read_text(encoding="utf-8")
for key in sys.argv[1:] or ["X", "Y", "Z"]:
    src = HERE / f"p3_{key}.src.html"
    if not src.exists():
        continue
    s = src.read_text(encoding="utf-8")
    s = s.replace("<!--CSS-->", f"<style>{css}</style>").replace("<!--DATA-->", f"<script>window.MOCK3={data};</script>")
    s = s.replace("<!--COMMON-->", f"<script>{common}</script>")
    out = HERE / f"ui_p3_{key}.html"
    out.write_text(s, encoding="utf-8")
    print(out.name, len(s.encode()) // 1024, "KB")
