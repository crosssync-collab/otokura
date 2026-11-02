"""LRC / Enhanced LRC(A2 拡張)のパーサ。

対応するもの
- 行時刻 [mm:ss] [mm:ss.xx] [mm:ss.xxx] [mm:ss:xx]、1 行に複数の時刻(サビの再利用)
- ID タグ [ar:] [ti:] [al:] [length:] [offset:+/-ms] など(offset は時刻に反映)
- 語の時刻 <mm:ss.xx>(Enhanced LRC)
- 歌い手の印: 行頭の "v1:" "v2:"(Apple 由来の eLRC)と "M:" "F:" "D:"(Walaoke)
- バックボーカル: 直前の行に続く "[bg: <..>語 <..>語]" 行
- 同じ時刻が 2 行続く形式は、2 行目を訳(translation レイヤー)として扱う
"""
from __future__ import annotations

import re

from .model import Agent, Layer, Line, Lyrics, Word, fill_ends

_TS = r"(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?"
LINE_TS = re.compile(r"\[" + _TS + r"\]")
WORD_TS = re.compile(r"<" + _TS + r">")
ID_TAG = re.compile(r"^\[([a-zA-Z#]+):(.*)\]\s*$")
BG_LINE = re.compile(r"^\[bg:(.*)\]\s*$", re.I)
AGENT_PREFIX = re.compile(r"^(v\d+|M|F|D)\s*:\s*", re.I)

WALAOKE = {"m": "v1", "f": "v2", "d": "v1000"}


def _ms(m: re.Match) -> int:
    mm, ss, frac = m.group(1), m.group(2), m.group(3)
    ms = int(mm) * 60000 + int(ss) * 1000
    if frac:
        ms += int(frac.ljust(3, "0")[:3])   # .5 → 500, .05 → 50, .005 → 5
    return ms


def looks_like_lrc(text: str) -> bool:
    """時刻付きの行が 1 行以上あり、かつ空でない行の半分以上を占めていれば LRC とみなす。"""
    lines = [l.strip() for l in text.splitlines()[:300] if l.strip()]
    timed = sum(1 for l in lines if LINE_TS.match(l))
    return timed >= 1 and timed * 2 >= len(lines)


def _words(body: str, offset: int) -> tuple[str, list[Word]]:
    """'<t>語 <t>語<t>' → (表示テキスト, 語リスト)。語の時刻が無ければ語リストは空。"""
    parts = WORD_TS.split(body)
    # split は [前置き, mm, ss, frac, 文字, mm, ss, frac, 文字, ...] を返す
    if len(parts) == 1:
        return body.strip(), []
    lead = parts[0]
    words: list[Word] = []
    for i in range(1, len(parts), 4):
        t = int(parts[i]) * 60000 + int(parts[i + 1]) * 1000
        if parts[i + 2]:
            t += int(parts[i + 2].ljust(3, "0")[:3])
        t -= offset
        txt = parts[i + 3]
        if words and not words[-1].end:
            words[-1].end = t
        if txt:
            words.append(Word(start=t, end=0, text=txt))
    if lead.strip() and words:
        words[0].text = lead + words[0].text
    text = "".join(w.text for w in words).strip()
    return text, words


def parse_lrc(text: str, source: str = "sidecar:lrc", track_ms: int | None = None) -> Lyrics:
    offset = 0
    meta: dict[str, str] = {}
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for l in raw:
        m = ID_TAG.match(l.strip())
        if m and not LINE_TS.match(l.strip()):
            k, v = m.group(1).lower(), m.group(2).strip()
            meta[k] = v
            if k == "offset":
                try:
                    offset = int(v.replace("+", ""))
                except ValueError:
                    pass

    entries: list[tuple[int, int, Line]] = []   # (時刻, 出現順, 行)
    agents: dict[str, Agent] = {}
    plain: list[str] = []
    order = 0
    last_line: Line | None = None

    for l in raw:
        s = l.strip()
        if not s:
            continue
        bg = BG_LINE.match(s)
        if bg and last_line is not None:
            _, ws = _words(bg.group(1), offset)
            last_line.bg = ws or [Word(start=last_line.start or 0, end=0, text=bg.group(1).strip())]
            continue
        stamps = []
        rest = s
        while True:
            m = LINE_TS.match(rest)
            if not m:
                break
            stamps.append(_ms(m) - offset)
            rest = rest[m.end():]
        if not stamps:
            if not ID_TAG.match(s):
                plain.append(s)
            continue
        agent = None
        am = AGENT_PREFIX.match(rest)
        if am:
            key = am.group(1).lower()
            agent = WALAOKE.get(key, key)
            rest = rest[am.end():]
            if agent not in agents:
                agents[agent] = Agent(id=agent, role="main" if agent == "v1" else "duet")
        body_text, words = _words(rest, offset)
        for st in stamps:
            ln = Line(start=max(0, st), end=None, text=body_text,
                      words=[Word(w.start, w.end, w.text) for w in words] if len(stamps) == 1 else [],
                      agent=agent)
            entries.append((ln.start, order, ln))
            order += 1
            last_line = ln

    if not entries:
        lines = [Line(start=None, end=None, text=t) for t in plain]
        return Lyrics(source=source, layers=[Layer(kind="main", synced=False, lines=lines)])

    entries.sort(key=lambda e: (e[0], e[1]))

    # 同じ時刻が連続 → 2 行目以降は訳
    main: list[Line] = []
    trans: list[Line] = []
    for t, _, ln in entries:
        if main and main[-1].start == t and ln.text and main[-1].text and ln.text != main[-1].text:
            trans.append(Line(start=t, end=None, text=ln.text))
        else:
            main.append(ln)

    # 空行は「前の行の終わり」として使い、表示行からは外す
    for i, ln in enumerate(main):
        if not ln.text and i > 0 and main[i - 1].end is None:
            main[i - 1].end = ln.start
    nonempty = [ln for ln in main if ln.text]
    fill_ends(nonempty, track_ms)

    layers = [Layer(kind="main", synced=True, lines=nonempty, agents=list(agents.values()),
                    lang=meta.get("la") or meta.get("lang"), offset_ms=offset)]
    if trans and len(trans) >= len(nonempty) * 0.3:
        by_start = {ln.start: ln for ln in nonempty}
        for tl in trans:
            src = by_start.get(tl.start)
            tl.end = src.end if src else None
        fill_ends(trans, track_ms)
        layers.append(Layer(kind="translation", synced=True, lines=trans))
    elif trans:   # 訳と言えるほど無い → 本文に戻す
        nonempty.extend(trans)
        nonempty.sort(key=lambda x: x.start)
    return Lyrics(source=source, layers=layers)


def parse_plain(text: str, source: str = "sidecar:txt") -> Lyrics:
    lines = [Line(start=None, end=None, text=t.strip()) for t in text.replace("\r\n", "\n").split("\n")]
    # 先頭と末尾の空行を落とし、連続空行は 1 つに(段落の区切りとして残す)
    out: list[Line] = []
    for ln in lines:
        if not ln.text and (not out or not out[-1].text):
            continue
        out.append(ln)
    while out and not out[-1].text:
        out.pop()
    return Lyrics(source=source, layers=[Layer(kind="main", synced=False, lines=out)])
