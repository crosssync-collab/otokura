"""タグの表示と書き込み(音楽ファイルに書く)。

形式ごとのタグ(Vorbis コメント / ID3 / MP4 / ASF / APEv2)を「キー → 値のリスト」にそろえて扱う。
- 基本の項目(曲名・アーティストなど)は形式ごとのキーに置き換えてから書く
- 書き込みは変わったキーだけ。書く前の値を返すので、呼び出し側が記録すれば元に戻せる
- 文字化け(CP932 を Latin-1 として読んだもの)は表示で直す。直した値を保存すると正しい文字で書き直される
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from .lyrics.encoding import fix_mojibake

FIELDS = ["title", "artist", "album", "albumartist", "date", "genre", "tracknumber", "discnumber", "composer", "comment"]
ALBUM_FIELDS = ["album", "albumartist", "date", "genre"]

_VORBIS = {f: f.upper() for f in FIELDS}
_ID3 = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2", "date": "TDRC", "genre": "TCON",
        "tracknumber": "TRCK", "discnumber": "TPOS", "composer": "TCOM", "comment": "COMM::eng"}
_MP4 = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART", "date": "\xa9day",
        "genre": "\xa9gen", "tracknumber": "trkn", "discnumber": "disk", "composer": "\xa9wrt", "comment": "\xa9cmt"}
_ASF = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "albumartist": "WM/AlbumArtist",
        "date": "WM/Year", "genre": "WM/Genre", "tracknumber": "WM/TrackNumber", "discnumber": "WM/PartOfSet",
        "composer": "WM/Composer", "comment": "Description"}
_APE = {"title": "Title", "artist": "Artist", "album": "Album", "albumartist": "Album Artist", "date": "Year",
        "genre": "Genre", "tracknumber": "Track", "discnumber": "Disc", "composer": "Composer", "comment": "Comment"}
_MAPS = {"vorbis": _VORBIS, "id3": _ID3, "mp4": _MP4, "asf": _ASF, "ape": _APE}


class TagError(Exception):
    pass


# ---------- 開く ----------
def _open(path: Path):
    import mutagen
    f = mutagen.File(str(path))
    if f is None:
        raise TagError("タグを読めない形式です")
    return f


def _kind(f) -> str:
    from mutagen.apev2 import APEv2
    from mutagen.asf import ASF
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    if isinstance(f, MP4):
        return "mp4"
    if isinstance(f, ASF):
        return "asf"
    tags = f.tags
    if tags is None:
        if type(f).__name__ in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC", "OggSpeex"):
            f.add_tags()
            return "vorbis"
        try:
            f.add_tags()
        except Exception as e:
            raise TagError(f"この形式にはタグを付けられません: {e}") from e
        tags = f.tags
    if isinstance(tags, ID3):
        return "id3"
    if isinstance(tags, APEv2):
        return "ape"
    if hasattr(tags, "as_dict") or type(f).__name__.startswith(("FLAC", "Ogg")):
        return "vorbis"
    raise TagError("この形式のタグは編集できません")


def _id3_ver(f) -> int:
    try:
        return 3 if f.tags.version[1] == 3 else 4
    except Exception:
        return 4


# ---------- 読む ----------
def _entries(f, kind: str) -> list[dict]:
    """タグの一覧。key はその形式のキー、values は文字列のリスト、editable は文字として書き換えられるか。"""
    tags, out = f.tags, []
    if tags is None:
        return out
    if kind == "vorbis":
        seen: dict[str, list[str]] = {}
        for k, v in tags:
            if k.lower() == "metadata_block_picture":
                continue
            seen.setdefault(k.upper(), []).append(v)
        out = [{"key": k, "values": v, "editable": True} for k, v in seen.items()]
    elif kind == "id3":
        for k, fr in tags.items():
            if fr.FrameID.startswith("T") or fr.FrameID == "COMM":
                out.append({"key": k, "values": [str(x) for x in fr.text], "editable": True})
            else:
                out.append({"key": k, "values": [_summary(fr)], "editable": False})
    elif kind == "mp4":
        from mutagen.mp4 import MP4FreeForm
        for k, v in tags.items():
            if not isinstance(v, list):                 # cpil・pgap などの真偽値や数値
                out.append({"key": k, "values": [str(v)], "editable": False})
            elif k in ("trkn", "disk"):
                out.append({"key": k, "values": [f"{n}/{t}" if t else str(n) for n, t in v], "editable": True})
            elif k == "covr":
                out.append({"key": k, "values": [f"画像 {len(v)} 枚"], "editable": False})
            elif all(isinstance(x, str) for x in v):
                out.append({"key": k, "values": list(v), "editable": True})
            elif all(isinstance(x, MP4FreeForm) for x in v):
                out.append({"key": k, "values": [bytes(x).decode("utf-8", "replace") for x in v], "editable": True})
            else:
                out.append({"key": k, "values": [", ".join(str(x) for x in v)], "editable": False})
    elif kind == "asf":
        from mutagen.asf import ASFUnicodeAttribute
        for k, v in tags.items():
            if all(isinstance(x, ASFUnicodeAttribute) for x in v):
                out.append({"key": k, "values": [str(x) for x in v], "editable": True})
            else:
                out.append({"key": k, "values": [_summary(v[0]) if v else ""], "editable": False})
    elif kind == "ape":
        from mutagen.apev2 import TEXT
        for k, v in tags.items():
            if v.kind == TEXT:
                out.append({"key": k, "values": list(v), "editable": True})
            else:
                out.append({"key": k, "values": [f"バイナリ {len(v.value):,} バイト"], "editable": False})
    return out


def _summary(x) -> str:
    data = getattr(x, "data", None)
    if isinstance(data, (bytes, bytearray)):
        if getattr(x, "mime", None):
            return f"画像 {x.mime} {max(1, len(data) // 1024):,} KB"
        return f"バイナリ {len(data):,} バイト"
    s = str(x)
    return s if len(s) <= 200 else s[:200] + "…"


def _field_key(kind: str, entries: dict[str, dict], field: str) -> str:
    key = _MAPS[kind][field]
    if kind == "id3" and field == "comment":       # 説明なしのコメント(言語は問わない)を使う
        for k in entries:
            if k.startswith("COMM::"):
                return k
    if kind in ("asf", "ape"):                     # 大文字小文字の違う既存のキーに合わせる
        for k in entries:
            if k.lower() == key.lower():
                return k
    return key


def read(path: Path) -> dict:
    f = _open(path)
    kind = _kind(f)
    entries = _entries(f, kind)
    by_key = {e["key"]: e for e in entries}
    fields = {}
    for name in FIELDS:
        e = by_key.get(_field_key(kind, by_key, name))
        v = e["values"][0] if e and e["values"] else ""
        fields[name] = fix_mojibake(v) if v else ""
    for e in entries:
        if e["editable"]:
            e["values"] = [fix_mojibake(v) for v in e["values"]]
    return {"kind": kind, "fields": fields, "tags": entries, "hasCover": _has_cover(f, kind)}


def _has_cover(f, kind: str) -> bool:
    if getattr(f, "pictures", None):
        return True
    t = f.tags
    if t is None:
        return False
    if kind == "id3":
        return bool(t.getall("APIC"))
    if kind == "mp4":
        return "covr" in t
    if kind == "vorbis":
        return any(k.lower() == "metadata_block_picture" for k, _ in t)
    if kind == "ape":
        return any(k.lower().startswith("cover art") for k in t.keys())
    if kind == "asf":
        return "WM/Picture" in t
    return False


# ---------- 書く ----------
def _get(f, kind: str, key: str) -> list[str] | None:
    for e in _entries(f, kind):
        if e["key"] == key and e["editable"]:
            return e["values"]
    return None


def _set(f, kind: str, key: str, values: list[str] | None) -> None:
    t = f.tags
    values = [v for v in (values or []) if v != ""] or None
    if kind == "vorbis":
        if values is None:
            if key in t:
                del t[key]
        else:
            t[key] = values
    elif kind == "id3":
        from mutagen import id3
        if key in t:
            del t[key]
        if values is None:
            return
        enc = 1 if _id3_ver(f) == 3 else 3
        fid = key.split(":", 1)[0]
        if fid == "TXXX":
            t.add(id3.TXXX(encoding=enc, desc=key.split(":", 1)[1], text=values))
        elif fid == "COMM":
            _, desc, lang = (key.split(":") + ["", "eng"])[:3]
            t.add(id3.COMM(encoding=enc, lang=lang or "eng", desc=desc, text=values))
        elif fid.startswith("T") and hasattr(id3, fid):
            t.add(getattr(id3, fid)(encoding=enc, text=values))
        else:
            raise TagError(f"{key} は文字として書けないタグです")
    elif kind == "mp4":
        from mutagen.mp4 import MP4FreeForm
        if values is None:
            if key in t:
                del t[key]
        elif key in ("trkn", "disk"):
            pairs = []
            for v in values:
                n, _, tot = v.partition("/")
                try:
                    pairs.append((int(n or 0), int(tot or 0)))
                except ValueError as e:
                    raise TagError(f"{'曲' if key == 'trkn' else 'ディスク'}番号は「3」や「3/12」の形で入れてください") from e
            t[key] = pairs
        elif key.startswith("----"):
            t[key] = [MP4FreeForm(v.encode("utf-8")) for v in values]
        else:
            t[key] = values
    elif kind == "asf":
        if values is None:
            if key in t:
                del t[key]
        else:
            t[key] = values
    elif kind == "ape":
        if values is None:
            if key in t:
                del t[key]
        else:
            t[key] = values


def _save(f, kind: str) -> None:
    if kind == "id3":
        f.save(v2_version=_id3_ver(f))
    else:
        f.save()


def write(path: Path, fields: dict[str, str] | None = None, tags: dict[str, list[str] | None] | None = None,
          cover: bytes | None = None, remove_cover: bool = False) -> dict:
    """変わったキーだけ書く。戻り値は {"before": {キー: 書く前の値 or None}, "cover": 書く前のカバー or None}。"""
    f = _open(path)
    kind = _kind(f)
    by_key = {e["key"]: e for e in _entries(f, kind)}
    changes: dict[str, list[str] | None] = {}
    for name, v in (fields or {}).items():
        if name not in FIELDS:
            raise TagError(f"知らない項目です: {name}")
        changes[_field_key(kind, by_key, name)] = [v.strip()] if v and v.strip() else None
    for k, v in (tags or {}).items():
        if k in by_key and not by_key[k]["editable"]:
            raise TagError(f"{k} は文字として書き換えられないタグです")
        if kind == "vorbis":
            k = k.upper()
        changes[k] = [x for x in v if x.strip()] if v else None
    before = {k: _get(f, kind, k) for k in changes}
    for k, v in changes.items():
        _set(f, kind, k, v)
    old_cover = None
    if cover is not None or remove_cover:
        old_cover = get_cover(path, f)
        _write_cover(f, kind, cover)
    _save(f, kind)
    return {"before": before, "cover": old_cover, "coverChanged": cover is not None or remove_cover}


# ---------- カバー ----------
def get_cover(path: Path, f=None) -> bytes | None:
    from .metadata import _picture
    f = f or _open(path)
    return _picture(f, f.tags)


def _mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    raise TagError("カバー画像は JPEG か PNG にしてください")


def _write_cover(f, kind: str, data: bytes | None) -> None:
    mime = _mime(data) if data else None
    t = f.tags
    if kind == "vorbis" and hasattr(f, "clear_pictures"):          # FLAC
        from mutagen.flac import Picture
        f.clear_pictures()
        if data:
            p = Picture()
            p.type, p.mime, p.data = 3, mime, data
            _size(p, data)
            f.add_picture(p)
    elif kind == "vorbis":                                          # Ogg
        from mutagen.flac import Picture
        for k in [k for k in t.keys() if k.lower() == "metadata_block_picture"]:
            del t[k]
        if data:
            p = Picture()
            p.type, p.mime, p.data = 3, mime, data
            _size(p, data)
            t["METADATA_BLOCK_PICTURE"] = [base64.b64encode(p.write()).decode("ascii")]
    elif kind == "id3":
        from mutagen.id3 import APIC
        t.delall("APIC")
        if data:
            t.add(APIC(encoding=0, mime=mime, type=3, desc="", data=data))
    elif kind == "mp4":
        from mutagen.mp4 import MP4Cover
        if "covr" in t:
            del t["covr"]
        if data:
            t["covr"] = [MP4Cover(data, MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG)]
    elif kind == "ape":
        from mutagen.apev2 import BINARY, APEValue
        for k in [k for k in t.keys() if k.lower().startswith("cover art")]:
            del t[k]
        if data:
            name = b"cover.png" if mime == "image/png" else b"cover.jpg"
            t["Cover Art (Front)"] = APEValue(name + b"\x00" + data, BINARY)
    else:
        raise TagError("この形式のカバー画像は編集できません")


def _size(pic, data: bytes) -> None:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        pic.width, pic.height = im.size
        pic.depth = 24
    except Exception:
        pass
