"""TTML(Apple Music 形式を含む)のパーサ。

- <p begin end ttm:agent> が行、<span begin end> が語(音節)
- <span ttm:role="x-bg"> はバックボーカル(中の span が語)
- <head> の <ttm:agent xml:id type> と <ttm:name> で歌い手
- Apple の <iTunesMetadata><translations>/<transliterations> を訳・読みレイヤーに
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .model import Agent, Layer, Line, Lyrics, Word, fill_ends

TT = "http://www.w3.org/ns/ttml"
TTM = "http://www.w3.org/ns/ttml#metadata"
XML = "http://www.w3.org/XML/1998/namespace"
ITUNES = "http://music.apple.com/lyric-ttml-internal"

_CLOCK = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")
_OFFSET = re.compile(r"^(\d+(?:\.\d+)?)(h|m|s|ms)$")


def parse_time(v: str | None) -> int | None:
    if not v:
        return None
    v = v.strip()
    m = _OFFSET.match(v)
    if m:
        n, u = float(m.group(1)), m.group(2)
        return round(n * {"h": 3600000, "m": 60000, "s": 1000, "ms": 1}[u])
    m = _CLOCK.match(v)
    if not m:
        return None
    a, b, s = m.groups()
    if b is not None:        # h:m:s
        h, mi = int(a), int(b)
    elif a is not None:      # m:s
        h, mi = 0, int(a)
    else:
        h, mi = 0, 0
    return round(((h * 60 + mi) * 60 + float(s)) * 1000)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: ET.Element, name: str, ns: str | None = None) -> str | None:
    if ns:
        return el.get(f"{{{ns}}}{name}")
    v = el.get(name)
    if v is None:   # 名前空間付きで書かれている場合
        for k, val in el.attrib.items():
            if _local(k) == name:
                return val
    return v


def _span_words(parent: ET.Element) -> list[Word]:
    words: list[Word] = []
    for ch in parent:
        if _local(ch.tag) != "span":
            continue
        if _attr(ch, "role", TTM) == "x-bg":
            continue
        b, e = parse_time(_attr(ch, "begin")), parse_time(_attr(ch, "end"))
        txt = "".join(ch.itertext())
        if b is None:
            # 時刻の無い span は中の span を語として扱う
            words.extend(_span_words(ch))
        else:
            words.append(Word(start=b, end=e or 0, text=txt))
        if ch.tail and words:
            # 改行を含む空白は整形用のインデントなので捨てる(日本語の音節間に空白を入れないため)
            if ch.tail.strip() or "\n" not in ch.tail:
                words[-1].text += ch.tail
    return words


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_ttml(text: str, source: str = "sidecar:ttml", track_ms: int | None = None) -> Lyrics:
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    lang = root.get(f"{{{XML}}}lang")

    agents: dict[str, Agent] = {}
    for el in root.iter():
        if _local(el.tag) == "agent" and el.get(f"{{{XML}}}id"):
            aid = el.get(f"{{{XML}}}id")
            name = next((_collapse("".join(n.itertext())) for n in el if _local(n.tag) == "name"), None)
            agents[aid] = Agent(id=aid, name=name or None,
                                role="main" if not agents else ("duet" if el.get("type") != "group" else "main"))

    lines: list[Line] = []
    keyed: dict[str, Line] = {}
    for p in root.iter(f"{{{TT}}}p"):
        b, e = parse_time(_attr(p, "begin")), parse_time(_attr(p, "end"))
        words = _span_words(p)
        bg: list[Word] = []
        for sp in p.iter(f"{{{TT}}}span"):
            if _attr(sp, "role", TTM) == "x-bg":
                bg = _span_words(sp) or [Word(start=b or 0, end=e or 0, text=_collapse("".join(sp.itertext())))]
        if words:
            text_ = _collapse("".join(w.text for w in words))
        else:
            # 語の時刻なし: bg 以外の文字を集める
            parts = [p.text or ""]
            for ch in p:
                if _attr(ch, "role", TTM) != "x-bg":
                    parts.append("".join(ch.itertext()))
                parts.append(ch.tail or "")
            text_ = _collapse("".join(parts))
        if not text_ and not bg:
            continue
        ln = Line(start=b, end=e, text=text_, words=words, agent=_attr(p, "agent", TTM), bg=bg)
        lines.append(ln)
        key = _attr(p, "key", ITUNES) or p.get(f"{{{XML}}}id")
        if key:
            keyed[key] = ln

    synced = any(l.start is not None for l in lines)
    if synced:
        lines = [l for l in lines if l.start is not None]
        lines.sort(key=lambda l: l.start)
        fill_ends(lines, track_ms)
        for ln in lines:
            for w in ln.bg:
                if not w.end:
                    w.end = ln.end
    used = {l.agent for l in lines if l.agent}
    layer_agents = [a for a in agents.values() if a.id in used]
    layers = [Layer(kind="main", lang=lang, synced=synced, lines=lines, agents=layer_agents)]

    # Apple の訳・読み
    for kind_tag, kind in (("translation", "translation"), ("transliteration", "pronunciation")):
        for tr in root.iter():
            if _local(tr.tag) != kind_tag:
                continue
            tl: list[Line] = []
            for t in tr:
                if _local(t.tag) != "text":
                    continue
                src = keyed.get(t.get("for") or "")
                if src is None:
                    continue
                tl.append(Line(start=src.start, end=src.end, text=_collapse("".join(t.itertext()))))
            if tl:
                layers.append(Layer(kind=kind, lang=tr.get(f"{{{XML}}}lang"), synced=synced, lines=tl))
    return Lyrics(source=source, layers=layers)
