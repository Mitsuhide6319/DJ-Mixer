"""ジャンルのまとまり（design_phase3.md §3.4）。タグの表記の揺れ（Jazz / Fusion / Jazz & Fusion / Smooth Jazz …）をまとめる。"""
from __future__ import annotations

import re

GROUPS: list[tuple[str, str]] = [   # 上から順に判定する
    ("ジャズ・フュージョン", r"jazz|fusion|bossa|swing|bebop"),
    ("R&B・ソウル・ファンク", r"r&b|rnb|soul|funk"),
    ("J-POP・歌謡", r"j-?pop|domestic|歌謡|kayo|japanese|邦楽"),
    ("ポップス", r"pop|adult contemporary|easy listening"),
    ("ロック", r"rock|metal|punk|alternative|blues"),
    ("ダンス・エレクトロ", r"dance|house|techno|electro|trance|club"),
    ("サントラ・ゲーム・アニメ", r"soundtrack|game|anime|ost|サントラ"),
    ("クラシック", r"classical|orchestra|クラシック"),
]
OTHER = "その他"
NAMES = [n for n, _ in GROUPS] + [OTHER]
_RX = [(n, re.compile(rx, re.I)) for n, rx in GROUPS]


def group_of(genre: str | None) -> str:
    if genre:
        for name, rx in _RX:
            if rx.search(genre):
                return name
    return OTHER
