"""音蔵の歌詞 内部形式。

OpenSubsonic songLyrics v2 の構造(line / cueLine / cue / agents / kind)に寄せてある。
時刻はすべてミリ秒の整数。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Kind = Literal["main", "translation", "pronunciation"]
Role = Literal["main", "duet", "bg"]


@dataclass
class Word:
    """語(または音節)。text は区切りの空白も含めた表示用文字列。"""
    start: int
    end: int
    text: str


@dataclass
class Line:
    start: int | None          # 同期なし歌詞では None
    end: int | None
    text: str
    words: list[Word] = field(default_factory=list)
    agent: str | None = None   # Agent.id
    bg: list[Word] = field(default_factory=list)  # バックボーカル(TTML x-bg)

    @property
    def bg_text(self) -> str:
        return "".join(w.text for w in self.bg).strip()


@dataclass
class Agent:
    id: str
    role: Role = "main"
    name: str | None = None


@dataclass
class Layer:
    kind: Kind = "main"
    lang: str | None = None
    synced: bool = False
    lines: list[Line] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    offset_ms: int = 0   # ファイル内の [offset:] 由来。表示時に加算済みではない

    @property
    def has_words(self) -> bool:
        return any(l.words for l in self.lines)


@dataclass
class Lyrics:
    source: str                 # sidecar:ttml / sidecar:lrc / sidecar:txt / tag:sylt / tag:text / lrclib / manual
    layers: list[Layer] = field(default_factory=list)
    instrumental: bool = False

    @property
    def main(self) -> Layer | None:
        return next((l for l in self.layers if l.kind == "main"), None)

    @property
    def synced(self) -> bool:
        m = self.main
        return bool(m and m.synced)

    @property
    def has_words(self) -> bool:
        m = self.main
        return bool(m and m.has_words)

    def to_json(self) -> dict:
        d = asdict(self)
        d["synced"] = self.synced
        d["hasWords"] = self.has_words
        return d

    @staticmethod
    def from_json(d: dict) -> "Lyrics":
        def words(ws):
            return [Word(**w) for w in ws or []]
        layers = []
        for L in d.get("layers", []):
            layers.append(Layer(
                kind=L.get("kind", "main"), lang=L.get("lang"), synced=L.get("synced", False),
                offset_ms=L.get("offset_ms", 0),
                agents=[Agent(**a) for a in L.get("agents", [])],
                lines=[Line(start=x.get("start"), end=x.get("end"), text=x.get("text", ""),
                            words=words(x.get("words")), agent=x.get("agent"), bg=words(x.get("bg")))
                       for x in L.get("lines", [])],
            ))
        return Lyrics(source=d.get("source", "manual"), layers=layers, instrumental=d.get("instrumental", False))


def fill_ends(lines: list[Line], track_ms: int | None = None) -> None:
    """end が無い行に、次の行の開始(最後は曲の長さ or +5秒)を入れる。語も同様。"""
    timed = [l for l in lines if l.start is not None]
    for i, l in enumerate(timed):
        nxt = timed[i + 1].start if i + 1 < len(timed) else None
        if l.end is None:
            if l.words and l.words[-1].end:
                l.end = l.words[-1].end
            elif nxt is not None:
                l.end = nxt
            else:
                l.end = track_ms if track_ms and track_ms > l.start else l.start + 5000
        for j, w in enumerate(l.words):
            if not w.end or w.end <= w.start:
                w.end = l.words[j + 1].start if j + 1 < len(l.words) else l.end
