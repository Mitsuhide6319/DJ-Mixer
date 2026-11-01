"""mockup/ui_mockup.src.html に実データ(mock_data.json)を埋め込み、単体で開ける ui_mockup.html を作る。
外部ファイルを読まないので、ダブルクリック(file://)でもそのまま表示できる。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = (ROOT / "mockup" / "ui_mockup.src.html").read_text(encoding="utf-8")
data = (ROOT / "work" / "mock_data.json").read_text(encoding="utf-8")
assert "<!--MOCK_DATA-->" in src
out = src.replace("<!--MOCK_DATA-->", "<script>window.MOCK=" + data + ";</script>")
(ROOT / "ui_mockup.html").write_text(out, encoding="utf-8")
print(f"ui_mockup.html {len(out.encode('utf-8')) // 1024} KB")
