"""Phase 2 モックアップ（案A・B・C）を、本番のテーマ・共通部品・実データを埋め込んだ単体 HTML にする（ダブルクリックで開ける）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
css = (ROOT / "frontend" / "theme.css").read_text(encoding="utf-8") + (HERE / "p2_common.css").read_text(encoding="utf-8")
data = (ROOT / "work" / "phase2" / "mock2_data.json").read_text(encoding="utf-8")
common = (HERE / "common.js").read_text(encoding="utf-8")
for key in sys.argv[1:] or ["A", "B", "C"]:
    src = HERE / f"p2_{key}.src.html"
    if not src.exists():
        continue
    s = src.read_text(encoding="utf-8")
    s = s.replace("<!--CSS-->", f"<style>{css}</style>").replace("<!--DATA-->", f"<script>window.MOCK2={data};</script>")
    s = s.replace("<!--COMMON-->", f"<script>{common}</script>")
    out = HERE / f"ui_p2_{key}.html"
    out.write_text(s, encoding="utf-8")
    print(out.name, len(s.encode()) // 1024, "KB")
