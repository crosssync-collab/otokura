"""CUE シート(アルバム 1 枚を 1 つの音声ファイルにまとめ、.cue に「何曲目が何分何秒から」を書いたもの)を読む。

- 文字コードは UTF-8(BOM あり・なし)、だめなら CP932(日本の取り込みに多い)
- FILE の名前と実際のファイルは、拡張子が違っても(.wav と書いて .flac にした等)名前が同じなら結びつける
- 曲は INDEX 01 から次の曲の INDEX 01 まで(最後の曲はファイルの終わりまで)。時刻は mm:ss:ff(1 秒 = 75 フレーム)
- 曲ごとのファイルに付く EAC の「Noncompliant」形式(次の曲の INDEX 00 だけ前のファイルにあり、INDEX 01 は次の FILE の中)は、
  INDEX 01 がある FILE の曲として数える。1 つの FILE に 2 曲以上ある場合だけ分ける
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .lyrics.encoding import decode_bytes

_TIME = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})")


@dataclass
class CueTrack:
    no: int
    title: str | None = None
    has01: bool = False            # このファイルの中に INDEX 01 がある
    performer: str | None = None
    start_ms: int = 0
    end_ms: int | None = None      # None = ファイルの終わりまで


@dataclass
class CueSheet:
    path: Path
    title: str | None = None
    performer: str | None = None
    date: str | None = None
    genre: str | None = None
    disc: int | None = None
    files: dict[str, list[CueTrack]] = field(default_factory=dict)   # FILE に書かれた名前 → 曲


def _val(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    return s.strip()


def parse(text: str, path: Path = Path("x.cue")) -> CueSheet:
    cs = CueSheet(path=path)
    cur_file: str | None = None
    tr: CueTrack | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        cmd = cmd.upper()
        if cmd == "FILE":
            m = re.match(r'\s*("([^"]*)"|(\S+))', rest)
            prev = cur_file
            cur_file = (m.group(2) if m.group(2) is not None else m.group(3)) if m else None
            if cur_file is not None:
                cs.files.setdefault(cur_file, [])
            if tr is not None and not tr.has01 and cur_file is not None and prev is not None:
                cs.files[prev].remove(tr)           # INDEX 01 がまだ無い曲は、次のファイルの曲(Noncompliant 形式)
                cs.files[cur_file].append(tr)
            else:
                tr = None
        elif cmd == "TRACK":
            parts = rest.split()
            if cur_file is None or not parts or not parts[0].isdigit():
                tr = None
                continue
            if len(parts) > 1 and parts[1].upper() != "AUDIO":
                tr = None
                continue
            tr = CueTrack(no=int(parts[0]))
            cs.files[cur_file].append(tr)
        elif cmd == "INDEX" and tr is not None:
            parts = rest.split()
            if len(parts) == 2 and parts[0] == "01":
                m = _TIME.fullmatch(parts[1])
                if m:
                    mm, ss, ff = (int(x) for x in m.groups())
                    tr.start_ms = mm * 60000 + ss * 1000 + round(ff * 1000 / 75)
                    tr.has01 = True
        elif cmd == "TITLE":
            if tr is not None:
                tr.title = _val(rest)
            elif cur_file is None:
                cs.title = _val(rest)
        elif cmd == "PERFORMER":
            if tr is not None:
                tr.performer = _val(rest)
            elif cur_file is None:
                cs.performer = _val(rest)
        elif cmd == "REM":
            k, _, v = rest.partition(" ")
            k = k.upper()
            if k == "DATE":
                cs.date = _val(v)
            elif k == "GENRE":
                cs.genre = _val(v)
            elif k == "DISCNUMBER" and _val(v).isdigit():
                cs.disc = int(_val(v))
    for name in list(cs.files):
        tracks = cs.files[name] = [t for t in cs.files[name] if t.has01]
        tracks.sort(key=lambda t: t.start_ms)
        for a, b in zip(tracks, tracks[1:]):
            a.end_ms = b.start_ms
    return cs


def read(path: Path) -> CueSheet | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > 1_000_000:
        return None
    return parse(decode_bytes(data), path)


def embedded(text: str | None, path: Path) -> tuple[CueSheet, list[CueTrack]] | None:
    """タグに埋め込まれた CUE シート。FILE の名前は取り込む前の .wav のことが多いので、いちばん曲の多い FILE を使う。"""
    if not text or "TRACK" not in text.upper():
        return None
    cs = parse(text, path)
    best = max(cs.files.values(), key=len, default=[])
    return (cs, best) if len(best) >= 2 else None


class CueIndex:
    """フォルダごとに .cue を読んでおき、音声ファイルに当てはまる曲を返す(1 回のスキャンの間だけ使う)。"""

    def __init__(self):
        self.dirs: dict[Path, list[CueSheet]] = {}

    def _sheets(self, d: Path) -> list[CueSheet]:
        if d not in self.dirs:
            out = []
            try:
                for p in sorted(d.iterdir()):
                    if p.suffix.lower() == ".cue" and p.is_file() and not p.name.startswith("._"):
                        cs = read(p)
                        if cs and cs.files:
                            out.append(cs)
            except OSError:
                pass
            self.dirs[d] = out
        return self.dirs[d]

    def find(self, audio: Path) -> tuple[CueSheet, list[CueTrack]] | None:
        """この音声ファイルを 2 曲以上に分ける CUE があれば (シート, 曲) を返す。"""
        name, stem = audio.name.lower(), audio.stem.lower()
        for cs in self._sheets(audio.parent):
            for fname, tracks in cs.files.items():
                if len(tracks) < 2:
                    continue
                f = Path(fname.replace("\\", "/")).name.lower()
                if f == name or Path(f).stem == stem:
                    return cs, tracks
        return None
