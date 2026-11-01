"""タグの文字化けの修正（design_phase3.md §2）。

古い形式のタグ（ID3v1・一部の ID3v2）の Shift-JIS が Latin-1 として読まれると「\x88À\x93¡…」のように化ける。
Latin-1 に戻して CP932 として読み直す。正しいアクセント付きの名前（Céline・Björk など）を壊さないよう、
(1) U+0080〜U+00FF の文字が 2 つ以上連続している（日本語の 2 バイト文字は高位のバイトが続く）、
(2) 読み直した結果にかな・漢字があり、私用領域・制御文字が無い、ときだけ直す。
"""
from __future__ import annotations

import re

_PAIR = re.compile(r"[\x80-\xff]{2}")
_JA = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
_BAD = re.compile(r"[-\x00-\x1f\x7f-\x9f�]")


def fix_mojibake(s: str | None) -> str | None:
    if not s or not _PAIR.search(s) or _JA.search(s):
        return s
    try:
        fixed = s.encode("latin-1").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    if not _JA.search(fixed) or _BAD.search(fixed):
        return s
    return fixed
