"""歌詞フォルダ(「アーティスト - 曲名.lrc / .txt / .ttml」が並ぶフォルダ)から探す。

音楽ファイルの横(サイドカー)とタグに歌詞が無い曲だけ使う。
名前は大文字小文字・全角半角・空白と記号の違いを無視して比べ、曲名の (Live) [Remaster] などの括弧書きは
無くても合うようにする。フォルダの中身が変わったら(フォルダの更新時刻で判断)作り直す。
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from pathlib import Path

from .model import Lyrics
from .resolver import SIDECAR_EXT, best, parse_file

CHECK_EVERY_S = 60
_BRACKETS = re.compile(r"\s*[\(\[（【［][^\)\]）】］]*[\)\]）】］]")
_FEAT = re.compile(r"\s*(?:feat\.?|ft\.|featuring|with|&|×|/|,|、)\s.*$|\s*[\(（](?:feat|ft)\.?.*$", re.I)
_CREDIT = re.compile(r"^(?:歌手|歌|唄|演唱|アーティスト|作詞|作词|作曲|編曲|编曲|词|曲|vocal|lyrics?|music|words?|written|"
                     r"composed|arranged|album|title|artist|singer|sing|by)\s*[:：\s　]", re.I)
HEADER_MAX = 8      # 先頭の何行までを曲名・歌手・作詞などの見出しとして落とすか


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(c for c in s if c.isalnum())


def _keys(artist: str, title: str) -> set[str]:
    arts = {norm(artist), norm(_FEAT.sub("", artist))}
    titles = {norm(title), norm(_BRACKETS.sub("", title))}
    return {f"{a}\0{t}" for a in arts for t in titles if a and t}


class LyricsFolder:
    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        self.lock = threading.Lock()
        self.index: dict[str, list[Path]] = {}
        self.stamp: tuple | None = None
        self.checked = 0.0

    def _stamp(self) -> tuple:
        out = []
        for d in self.dirs:
            for p in [d, *(x for x in d.rglob("*") if x.is_dir())] if d.is_dir() else []:
                try:
                    out.append((str(p), p.stat().st_mtime))
                except OSError:
                    pass
        return tuple(out)

    def _rebuild(self) -> None:
        index: dict[str, list[Path]] = {}
        for d in self.dirs:
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if p.suffix.lower() not in SIDECAR_EXT or not p.is_file():
                    continue
                artist, sep, title = p.stem.partition(" - ")
                if not sep:
                    continue
                for k in _keys(artist, title):
                    index.setdefault(k, []).append(p)
        self.index = index

    def _fresh(self) -> None:
        now = time.time()
        if now - self.checked < CHECK_EVERY_S and self.stamp is not None:
            return
        self.checked = now
        stamp = self._stamp()
        if stamp != self.stamp:
            self._rebuild()
            self.stamp = stamp

    def find(self, artist: str | None, title: str | None) -> list[Path]:
        if not self.dirs or not artist or not title:
            return []
        with self.lock:
            self._fresh()
            found: dict[Path, None] = {}
            for k in _keys(artist, title):
                for p in self.index.get(k, []):
                    found[p] = None
        return sorted(found)

    def load(self, paths: list[Path], title: str | None, track_ms: int | None = None,
             artist: str | None = None) -> Lyrics | None:
        cands = []
        for p in paths:
            ly = parse_file(p, track_ms)
            if ly is None:
                continue
            ly.source = "folder:" + ly.source.split(":", 1)[-1]
            _strip_header(ly, title, artist)
            cands.append(ly)
        return best(cands)


def _strip_header(ly: Lyrics, title: str | None, artist: str | None = None) -> None:
    """先頭にある「曲名」「歌手名」「作詞 ○○」「Album: ○○」などの行を落とす(同期ありの .lrc にもある)。"""
    lines = ly.main.lines if ly.main else []
    names = {norm(title), norm(_BRACKETS.sub("", title or "")), norm(artist)} - {""}

    def is_header(text: str) -> bool:
        return not text or norm(text) in names or norm(_BRACKETS.sub("", text)) in names or bool(_CREDIT.match(text))

    i = 0
    while i < min(len(lines), HEADER_MAX) and is_header(lines[i].text):
        i += 1
    del lines[:i]
