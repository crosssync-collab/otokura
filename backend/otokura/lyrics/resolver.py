"""1 曲分の歌詞を手元から探して、一番良いものを選ぶ。

優先順位(上ほど優先)
1. 同期あり(さらに語の時刻ありを優先)
2. 置き場所: サイドカー .ttml > .lrc > 埋め込みタグ > .txt
LRCLIB と手動登録は DB 側(store.py)で扱う。
"""
from __future__ import annotations

from pathlib import Path

from .encoding import decode_bytes
from .lrc import looks_like_lrc, parse_lrc, parse_plain
from .model import Lyrics
from .tags import read_tag_lyrics
from .ttml import parse_ttml

SIDECAR_EXT = (".ttml", ".lrc", ".txt")
_PRIORITY = {"sidecar:ttml": 0, "sidecar:lrc": 1, "tag:sylt": 2, "tag:lrc": 2, "tag:text": 3,
             "sidecar:txt": 4, "folder:ttml": 5, "folder:lrc": 5, "folder:txt": 6, "lrclib": 7, "manual": -1}


def find_sidecars(audio: Path) -> dict[str, Path]:
    """同じフォルダの「同じ名前 + 歌詞拡張子」を大文字小文字を無視して探す。"""
    out: dict[str, Path] = {}
    try:
        siblings = list(audio.parent.iterdir())
    except OSError:
        return out
    stem = audio.stem.lower()
    for p in siblings:
        if p.is_file() and p.stem.lower() == stem and p.suffix.lower() in SIDECAR_EXT:
            out[p.suffix.lower()] = p
    return out


def parse_file(path: Path, track_ms: int | None = None) -> Lyrics | None:
    text = decode_bytes(path.read_bytes())
    ext = path.suffix.lower()
    try:
        if ext == ".ttml" or text.lstrip().startswith("<?xml") or "<tt " in text[:500]:
            return parse_ttml(text, track_ms=track_ms)
        if ext == ".lrc" or looks_like_lrc(text):
            return parse_lrc(text, source="sidecar:lrc", track_ms=track_ms)
        return parse_plain(text, source="sidecar:txt")
    except Exception:
        return None


def rank(ly: Lyrics) -> tuple:
    empty = not (ly.main and any(l.text for l in ly.main.lines))
    return (empty, not ly.synced, not ly.has_words, _PRIORITY.get(ly.source, 9))


def best(cands: list[Lyrics]) -> Lyrics | None:
    cands = [c for c in cands if c and c.main and any(l.text for l in c.main.lines)]
    return min(cands, key=rank) if cands else None


def resolve_local(audio: str | Path, track_ms: int | None = None) -> tuple[Lyrics | None, list[Path]]:
    """(選ばれた歌詞, 監視すべきサイドカーのパス)を返す。"""
    audio = Path(audio)
    side = find_sidecars(audio)
    cands: list[Lyrics] = []
    for p in side.values():
        ly = parse_file(p, track_ms)
        if ly:
            cands.append(ly)
    cands.extend(read_tag_lyrics(audio, track_ms))
    return best(cands), list(side.values())
