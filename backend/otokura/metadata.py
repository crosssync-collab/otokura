"""1 ファイルのタグと音声情報を読む(mutagen)。文字化け修復と、タグが無いときの推測もここで行う。"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path

from .lyrics.encoding import fix_mojibake

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff",
             ".ape", ".tta", ".wv", ".dsf", ".dff", ".mpc", ".wma"}
# ブラウザがそのまま再生できる形式(これ以外は transcode.py で変換して配る)
BROWSER_PLAYABLE = {"FLAC", "MP3", "AAC", "Vorbis", "Opus", "WAV"}
MIME = {"FLAC": "audio/flac", "MP3": "audio/mpeg", "AAC": "audio/mp4", "ALAC": "audio/mp4", "Vorbis": "audio/ogg",
        "Opus": "audio/ogg", "WAV": "audio/wav", "AIFF": "audio/aiff"}   # ALAC・AIFF は Safari ならそのまま鳴る

KEYS = {
    "title": ("title", "tit2", "\xa9nam"),
    "artist": ("artist", "tpe1", "\xa9art"),
    "album": ("album", "talb", "\xa9alb"),
    "albumartist": ("albumartist", "album artist", "album_artist", "tpe2", "aart"),
    "date": ("date", "year", "originaldate", "tdrc", "tyer", "\xa9day"),
    "genre": ("genre", "tcon", "\xa9gen"),
    "tracknumber": ("tracknumber", "track", "trck", "trkn"),
    "discnumber": ("discnumber", "disc", "disk", "tpos"),
}


@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str | None
    albumartist: str | None
    year: int | None
    genre: str | None
    track_no: int | None
    disc_no: int | None
    duration_ms: int
    codec: str
    sample_rate: int | None = None
    bit_depth: int | None = None
    bitrate: int | None = None
    fixed: bool = False       # 文字化けを直した
    guessed: bool = False     # ファイル名・フォルダ名から補った
    picture: bytes | None = field(default=None, repr=False)


def _first(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
        if v is None:
            return None
    if isinstance(v, tuple):                              # MP4 の trkn/disk = (番号, 総数)
        v = v[0]
    if hasattr(v, "text") and not isinstance(v, str):     # ID3 フレーム
        t = v.text
        v = t[0] if isinstance(t, list) and t else t
    try:
        s = str(v)
    except Exception:
        return None
    s = s.replace("\x00", " ").strip()
    return s or None


def _int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"\s*(\d{1,4})", s)
    return int(m.group(1)) if m else None


def _codec(f) -> str:
    n = type(f).__name__
    if n == "MP4":
        c = str(getattr(f.info, "codec", "") or "")
        return "ALAC" if c.startswith("alac") else "AAC"
    return {"FLAC": "FLAC", "MP3": "MP3", "EasyMP3": "MP3", "OggVorbis": "Vorbis", "OggOpus": "Opus", "OggFLAC": "FLAC",
            "WAVE": "WAV", "AIFF": "AIFF", "MonkeysAudio": "APE", "TrueAudio": "TTA", "EasyTrueAudio": "TTA",
            "WavPack": "WavPack", "DSF": "DSD", "DSDIFF": "DSD", "Musepack": "MPC", "ASF": "WMA"}.get(n, n)


def _tag_map(tags) -> dict[str, object]:
    """キーを小文字に揃えた辞書。ID3 はフレーム ID で入る。"""
    out: dict[str, object] = {}
    if tags is None:
        return out
    try:
        items = tags.items()
    except Exception:
        return out
    for k, v in items:
        key = str(k).lower()
        if key.startswith("txxx:"):
            key = key[5:]
        elif ":" in key and len(key) > 4 and key[:4].isalnum():
            key = key.split(":")[0]           # 'apic:cover' など
        out.setdefault(key, v)
    return out


def _picture(f, tags) -> bytes | None:
    try:
        pics = getattr(f, "pictures", None)            # FLAC
        if pics:
            front = [p for p in pics if p.type == 3] or pics
            return bytes(front[0].data)
        if tags is None:
            return None
        if hasattr(tags, "getall"):                    # ID3
            ap = tags.getall("APIC")
            if ap:
                front = [p for p in ap if p.type == 3] or ap
                return bytes(front[0].data)
        if "covr" in tags:                             # MP4
            return bytes(tags["covr"][0])
        for k in ("metadata_block_picture",):          # Ogg
            for key in list(tags.keys()):
                if str(key).lower() == k:
                    from mutagen.flac import Picture
                    return bytes(Picture(base64.b64decode(tags[key][0])).data)
        for key in list(tags.keys()):                  # APEv2
            if str(key).lower().startswith("cover art"):
                raw = tags[key].value
                i = raw.find(b"\x00")
                return bytes(raw[i + 1:] if i >= 0 else raw)
    except Exception:
        return None
    return None


_LEAD_NO = re.compile(r"^\s*(?:(\d{1,2})-(\d{1,3})|(\d{1,3}))(?:\s*[-._)\]]\s*|\s+)(?=\S)")
_YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")


def guess_from_path(rel: Path) -> dict:
    """'アーティスト/アルバム (2020)/01 - 曲名.flac' などから推測する。"""
    stem = rel.stem
    out: dict = {}
    m = _LEAD_NO.match(stem)
    if m:
        if m.group(3):
            out["track_no"] = int(m.group(3))
        else:
            out["disc_no"], out["track_no"] = int(m.group(1)), int(m.group(2))
        stem = stem[m.end():]
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if len(parts) >= 2:
        out["artist"], out["title"] = parts[0], " - ".join(parts[1:])
    else:
        out["title"] = stem.strip() or rel.stem
    folders = rel.parts[:-1]
    if folders:
        album_dir = folders[-1]
        if re.fullmatch(r"(?i)(cd|disc|disk)\s*\d+", album_dir.strip()) and len(folders) >= 2:   # 'CD1' フォルダ
            out["disc_no"] = int(re.sub(r"\D", "", album_dir))
            album_dir = folders[-2]
            folders = folders[:-1]
        y = _YEAR.search(album_dir)
        if y:
            out["year"] = int(y.group(1))
            album_dir = _YEAR.sub("", album_dir).strip(" -_")
        out["album"] = album_dir
        if len(folders) >= 2:
            out.setdefault("artist", folders[-2])
            out["albumartist"] = folders[-2]
    return out


def read(path: Path, rel: Path) -> TrackInfo | None:
    import mutagen
    try:
        f = mutagen.File(str(path))
    except Exception:
        f = None
    if f is None:
        return None
    tags = f.tags
    tm = _tag_map(tags)
    fixed = False
    vals: dict[str, str | None] = {}
    for name, aliases in KEYS.items():
        v = None
        for a in aliases:
            if a in tm:
                v = _first(tm[a])
                if v:
                    break
        if v:
            fv = fix_mojibake(v)
            fixed |= fv != v
            v = fv
        vals[name] = v
    g = guess_from_path(rel)
    guessed = not vals["title"]
    info = f.info
    length = float(getattr(info, "length", 0) or 0)
    bitrate = getattr(info, "bitrate", None)
    return TrackInfo(
        title=vals["title"] or g.get("title") or rel.stem,
        artist=vals["artist"] or vals["albumartist"] or g.get("artist") or "不明なアーティスト",
        album=vals["album"] or g.get("album"),
        albumartist=vals["albumartist"] or (None if vals["artist"] else g.get("albumartist")),
        year=_int(vals["date"]) or g.get("year"),
        genre=vals["genre"],
        track_no=_int(vals["tracknumber"]) or g.get("track_no"),
        disc_no=_int(vals["discnumber"]) or g.get("disc_no"),
        duration_ms=int(length * 1000),
        codec=_codec(f),
        sample_rate=getattr(info, "sample_rate", None),
        bit_depth=getattr(info, "bits_per_sample", None) or getattr(info, "bits_per_raw_sample", None),
        bitrate=int(bitrate / 1000) if bitrate else None,
        fixed=fixed,
        guessed=guessed,
        picture=_picture(f, tags),
    )
