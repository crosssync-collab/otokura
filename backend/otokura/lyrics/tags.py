"""埋め込みタグから歌詞を読む(mutagen)。

- MP3 / TTA / AIFF など ID3: SYLT(同期あり。時刻単位がミリ秒のものだけ)、USLT
- FLAC / Ogg / Opus(Vorbis コメント): LYRICS / UNSYNCEDLYRICS / SYNCEDLYRICS
- M4A: ©lyr
- APE / WavPack / MPC(APEv2): Lyrics / UNSYNCEDLYRICS
中身が LRC 書式なら同期歌詞として扱う。文字化けは CP932 として修復を試す。
"""
from __future__ import annotations

from pathlib import Path

from .encoding import fix_mojibake
from .lrc import looks_like_lrc, parse_lrc, parse_plain
from .model import Layer, Line, Lyrics, fill_ends

TEXT_KEYS = ("syncedlyrics", "lyrics", "unsyncedlyrics", "unsynced lyrics", "lyric")


def _from_text(text: str, track_ms: int | None) -> Lyrics | None:
    text = fix_mojibake(text or "").strip()
    if not text:
        return None
    if looks_like_lrc(text):
        return parse_lrc(text, source="tag:lrc", track_ms=track_ms)
    return parse_plain(text, source="tag:text")


def _sylt(frame, track_ms: int | None) -> Lyrics | None:
    if getattr(frame, "format", 2) != 2:      # 1 = MPEG フレーム単位は扱わない
        return None
    lines = [Line(start=int(t), end=None, text=fix_mojibake(str(txt)).strip())
             for txt, t in frame.text if str(txt).strip()]
    if not lines:
        return None
    lines.sort(key=lambda l: l.start)
    fill_ends(lines, track_ms)
    return Lyrics(source="tag:sylt", layers=[Layer(kind="main", synced=True, lines=lines,
                                                   lang=getattr(frame, "lang", None) or None)])


def read_tag_lyrics(path: str | Path, track_ms: int | None = None) -> list[Lyrics]:
    """見つかった歌詞をすべて返す(優先順位づけは resolver が行う)。"""
    try:
        import mutagen
        f = mutagen.File(str(path))
    except Exception:
        return []
    if f is None or f.tags is None:
        return []
    if track_ms is None and getattr(f, "info", None) and getattr(f.info, "length", None):
        track_ms = int(f.info.length * 1000)
    tags = f.tags
    found: list[Lyrics] = []

    # ID3
    getall = getattr(tags, "getall", None)
    if callable(getall):
        try:
            for fr in getall("SYLT"):
                ly = _sylt(fr, track_ms)
                if ly:
                    found.append(ly)
            for fr in getall("USLT"):
                ly = _from_text(str(fr.text), track_ms)
                if ly:
                    found.append(ly)
            for fr in getall("TXXX"):
                if fr.desc.lower() in TEXT_KEYS:
                    ly = _from_text("\n".join(map(str, fr.text)), track_ms)
                    if ly:
                        found.append(ly)
        except Exception:
            pass
        if found:
            return found

    # MP4(Vorbis コメントは「©」入りのキーを問い合わせると例外になるので型で分ける)
    from mutagen.mp4 import MP4Tags
    if isinstance(tags, MP4Tags):
        for v in tags.get("\xa9lyr", []):
            ly = _from_text(str(v), track_ms)
            if ly:
                found.append(ly)
        return found

    # Vorbis コメント / APEv2(キーの大文字小文字を無視)
    try:
        items = list(tags.items())
    except Exception:
        items = []
    for k, v in items:
        if str(k).lower() not in TEXT_KEYS:
            continue
        vals = v if isinstance(v, list) else [v]
        for x in vals:
            ly = _from_text(str(x), track_ms)
            if ly:
                found.append(ly)
    return found
