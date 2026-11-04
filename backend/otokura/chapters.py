"""MP4・M4A のチャプター(ライブ映像や 1 ファイルのアルバムに多い)を読み、CUE シートと同じ形にして曲に分ける。

- 読むのは ffprobe(Nero の chpl と QuickTime のテキストトラックの両方を読める)
- 2 つ以上のチャプターがあるときだけ分ける。長さ 0 のチャプターは飛ばす
- 映像だけのファイルは対象外。曲の区間は CUE の曲と同じく `元のファイル#曲番号` と cue_start / cue_end で持つ
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .cue import CueSheet, CueTrack

EXT = {".mp4", ".m4a"}
TIMEOUT_S = 60


def parse(data: dict, path: Path) -> tuple[CueSheet, list[CueTrack]] | None:
    """ffprobe の JSON → (シート, 曲)。"""
    if not any(s.get("codec_type") == "audio" for s in data.get("streams") or []):
        return None
    tracks: list[CueTrack] = []
    for ch in data.get("chapters") or []:
        try:
            start, end = float(ch["start_time"]), float(ch["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        title = str((ch.get("tags") or {}).get("title") or "").strip() or None
        tracks.append(CueTrack(no=len(tracks) + 1, title=title, has01=True, start_ms=round(start * 1000)))
    if len(tracks) < 2:
        return None
    for a, b in zip(tracks, tracks[1:]):
        a.end_ms = b.start_ms
    tags = {str(k).lower(): v for k, v in ((data.get("format") or {}).get("tags") or {}).items()}
    # アルバムのタグが無ければ、ファイルそのものを 1 枚のアルバムとして扱う(フォルダ名でまとめない)
    return CueSheet(path=path, title=tags.get("album") or tags.get("title") or path.stem), tracks


def read(path: Path) -> tuple[CueSheet, list[CueTrack]] | None:
    probe = shutil.which("ffprobe")
    if not probe or path.suffix.lower() not in EXT:
        return None
    try:
        r = subprocess.run([probe, "-v", "error", "-show_chapters", "-show_entries", "stream=codec_type:format_tags",
                            "-of", "json", str(path)], capture_output=True, timeout=TIMEOUT_S)
        return parse(json.loads(r.stdout or b"{}"), path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def video_frame(path: Path, at_s: float = 30.0) -> bytes | None:
    """カバーの無いライブ映像は、映像の 1 コマをカバーにする。映像が無ければ None。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    for t in (at_s, 0.0):       # 短い映像は 30 秒の位置が無いので先頭で撮り直す
        try:
            r = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-ss", str(t), "-i", str(path), "-map", "0:v:0",
                                "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                               capture_output=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0 and r.stdout:
            return r.stdout
    return None
