"""ブラウザがそのまま鳴らせない形式を ffmpeg で変換して配る。

- 可逆(ALAC・AIFF・APE・TTA・WavPack・DSD)は FLAC に、非可逆(WMA・MPC)は AAC 256kbps(m4a)にする
- DSD は PCM 24bit・88.2kHz にする(ブラウザも Chromecast も DSD は鳴らせない)
- 一度変換したものは data/transcode に残し、2 回目からはすぐ配る(途中から再生=Range も効く)。
  元のファイルが変わったら(更新時刻・大きさ)別のものとして作り直す。合計が上限を超えたら古いものから消す
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

LOSSY = {"WMA", "MPC"}
CODECS = {"ALAC", "AIFF", "APE", "TTA", "WavPack", "DSD", "WMA", "MPC"}   # 変換して鳴らす形式
TIMEOUT_S = 600


def target(codec: str) -> tuple[str, str, str]:
    """(変換後の形式名, 拡張子, MIME)"""
    return ("AAC", ".m4a", "audio/mp4") if codec in LOSSY else ("FLAC", ".flac", "audio/flac")


class Transcoder:
    def __init__(self, cache_dir: Path, max_bytes: int = 20 * 1024 ** 3):
        self.dir = Path(cache_dir)
        self.max_bytes = max_bytes
        self.ffmpeg = shutil.which("ffmpeg")
        self.lock = threading.Lock()
        self.busy: dict[str, threading.Event] = {}   # 変換中のもの(同じ曲を 2 つ同時に変換しない)

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    def cache_path(self, src: Path, codec: str) -> Path:
        st = src.stat()
        key = hashlib.sha1(f"{src}\0{st.st_mtime_ns}\0{st.st_size}".encode("utf-8")).hexdigest()[:24]
        return self.dir / (key + target(codec)[1])

    def ready(self, src: Path, codec: str) -> bool:
        return self.cache_path(src, codec).is_file()

    def get(self, src: Path, codec: str) -> Path:
        """変換したファイルのパス(無ければ変換してから返す)。"""
        dst = self.cache_path(src, codec)
        while True:
            if dst.is_file():
                try:
                    os.utime(dst)            # 使った順を残す(古いものから消すため)
                except OSError:
                    pass
                return dst
            with self.lock:
                ev = self.busy.get(dst.name)
                mine = ev is None
                if mine:
                    ev = self.busy[dst.name] = threading.Event()
            if not mine:
                ev.wait(TIMEOUT_S)
                if not dst.is_file():
                    raise RuntimeError("変換できませんでした")
                continue
            try:
                self._run(src, dst, codec)
            finally:
                with self.lock:
                    self.busy.pop(dst.name, None)
                ev.set()
            self._prune()

    def prepare(self, src: Path, codec: str) -> None:
        """次の曲を先に変換しておく(裏で動かす)。"""
        if self.ready(src, codec):
            return
        threading.Thread(target=lambda: self._quiet_get(src, codec), daemon=True).start()

    def _quiet_get(self, src: Path, codec: str) -> None:
        try:
            self.get(src, codec)
        except Exception:
            pass

    def _run(self, src: Path, dst: Path, codec: str) -> None:
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg がありません")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        cmd = [self.ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-map_metadata", "-1"]
        if codec in LOSSY:
            cmd += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "mp4"]
        else:
            if codec == "DSD":
                cmd += ["-ar", "88200", "-sample_fmt", "s32"]
            cmd += ["-c:a", "flac", "-compression_level", "5", "-f", "flac"]
        r = subprocess.run(cmd + [str(tmp)], capture_output=True, timeout=TIMEOUT_S)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("変換できませんでした: " + r.stderr.decode("utf-8", "replace")[-300:])
        tmp.replace(dst)

    def _prune(self) -> None:
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.dir.iterdir()
                     if p.is_file() and not p.name.endswith(".part")]
        except OSError:
            return
        total = sum(s for _, s, _ in files)
        for _, size, p in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
