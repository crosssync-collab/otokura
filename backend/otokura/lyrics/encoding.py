"""文字コードの判定と、CP932 文字化けの修復。

- サイドカー(.lrc/.txt/.ttml)はバイト列から判定: BOM → UTF-8 → CP932 → latin-1。
- 埋め込みタグは mutagen が latin-1 として読んだ「化けた文字列」が来るので、
  latin-1 に戻して CP932 として読み直せるかを試す(foobar2000 の自動修復と同じ考え方)。
"""
from __future__ import annotations

import re
import unicodedata

LEGACY = "cp932"

_JA = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")
_HIGH_LATIN1 = re.compile(r"[\u0080-ÿ]")


def decode_bytes(data: bytes, legacy: str = LEGACY) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(legacy)
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _ja_ratio(s: str) -> float:
    letters = [c for c in s if not c.isspace() and unicodedata.category(c)[0] in "LN"]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _JA.match(c)) / len(letters)


def fix_mojibake(s: str, legacy: str = LEGACY) -> str:
    """latin-1 として誤読された CP932 文字列を直す。直せない・直す必要がないときは元のまま。"""
    if not s or not _HIGH_LATIN1.search(s) or _JA.search(s):
        return s
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s
    try:
        fixed = raw.decode(legacy)
    except UnicodeDecodeError:
        return s
    # 本当に日本語になったときだけ採用(é などの正当なラテン文字を壊さない)
    return fixed if _ja_ratio(fixed) >= 0.3 else s
