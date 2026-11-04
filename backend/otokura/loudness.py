"""ffmpeg で曲の音量(ラウドネス)と、頭・お尻の無音を測る。音楽ファイルには書き込まない。

- ReplayGain のタグが無い曲の音量調整と、スマートフェードのつなぐ位置に使う
- 結果は loudness 表に (source, rel) で持つ。ファイルが変わったら(更新時刻・大きさ)測り直す
- NAS の負担を抑えるため、まとめて測るときは 1 曲ずつ nice 19 で動かす
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("otokura.loudness")
REFERENCE_LUFS = -18.0     # ReplayGain 2.0 の基準
SILENCE_DB = -50
SILENCE_MIN_S = 0.3
TIMEOUT_S = 600

_I = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_SIL_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SIL_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def parse(stderr: str, duration_s: float) -> dict | None:
    """ffmpeg のログ → {'lufs', 'head_ms', 'tail_ms'}。測れなければ None。"""
    found = _I.findall(stderr)
    if not found:
        return None
    lufs = float(found[-1])          # 最後の Summary の値
    spans: list[list[float | None]] = []
    for line in stderr.splitlines():
        m = _SIL_START.search(line)
        if m:
            spans.append([max(0.0, float(m.group(1))), None])
            continue
        m = _SIL_END.search(line)
        if m and spans:
            spans[-1][1] = float(m.group(1))
    head = tail = 0.0
    if spans and spans[0][0] <= 0.05 and spans[0][1] is not None:
        head = spans[0][1]
    if spans:
        start, end = spans[-1]
        if start > head and (end is None or end >= duration_s - 0.2):
            tail = max(0.0, duration_s - start)
    return {"lufs": lufs, "head_ms": round(head * 1000), "tail_ms": round(tail * 1000)}


def measure(path: Path, duration_ms: int, cut: tuple[int, int | None] | None = None, low_priority: bool = False) -> dict | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-nostats"]
    if low_priority and shutil.which("nice"):
        cmd = ["nice", "-n", "19", *cmd]
    if cut and cut[0]:
        cmd += ["-ss", f"{cut[0] / 1000:.3f}"]
    cmd += ["-i", str(path)]
    if cut and cut[1] is not None:
        cmd += ["-t", f"{(cut[1] - cut[0]) / 1000:.3f}"]
    cmd += ["-map", "0:a:0", "-af", f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_MIN_S},ebur128=framelog=quiet", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("音量を測れませんでした: %s: %s", path, e)
        return None
    return parse(r.stderr.decode("utf-8", "replace"), duration_ms / 1000)


class Loudness:
    """曲の測定と保存。measure_track は API から(次の曲など)、start はまとめて測るときに使う。"""

    def __init__(self, db, track_path):
        self.db, self.track_path = db, track_path
        self.gate = threading.BoundedSemaphore(2)     # API から同時に測るのは 2 曲まで
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.state = {"running": False, "done": 0, "total": 0, "errors": 0}

    def cached(self, r) -> dict | None:
        row = self.db.one("SELECT lufs,head_ms,tail_ms FROM loudness WHERE source=? AND rel=? AND mtime=? AND size=?",
                          (r["source"], r["rel"], r["mtime"], r["size"]))
        return dict(row) if row else None

    def measure_track(self, r, low_priority: bool = False) -> dict | None:
        hit = self.cached(r)
        if hit:
            return hit
        cut = (r["cue_start"], r["cue_end"]) if r["cue_start"] is not None else None
        with self.gate:
            res = measure(self.track_path(r), r["duration_ms"], cut, low_priority)
        if res:
            with self.db.write_lock:
                self.db.conn.execute(
                    "INSERT INTO loudness(source,rel,mtime,size,lufs,head_ms,tail_ms) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(source,rel) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,lufs=excluded.lufs,"
                    "head_ms=excluded.head_ms,tail_ms=excluded.tail_ms",
                    (r["source"], r["rel"], r["mtime"], r["size"], res["lufs"], res["head_ms"], res["tail_ms"]))
                self.db.conn.commit()
        return res

    def pending(self) -> list:
        return self.db.q("SELECT t.* FROM tracks t LEFT JOIN loudness l ON l.source=t.source AND l.rel=t.rel "
                         "AND l.mtime=t.mtime AND l.size=t.size WHERE l.lufs IS NULL ORDER BY t.id")

    def public(self) -> dict:
        total = self.db.one("SELECT COUNT(*) n FROM tracks")["n"]
        done = self.db.one("SELECT COUNT(*) n FROM tracks t JOIN loudness l ON l.source=t.source AND l.rel=t.rel "
                           "AND l.mtime=t.mtime AND l.size=t.size")["n"]
        return {**self.state, "tracks": total, "measured": done}

    def start(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="loudness").start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """同じスレッドで最後まで測る(テスト用)。"""
        self._lock.acquire()
        self._run()

    def _run(self) -> None:
        try:
            rows = self.pending()
            self.state = {"running": True, "done": 0, "total": len(rows), "errors": 0, "startedAt": time.time()}
            for r in rows:
                if self._stop.is_set():
                    break
                try:
                    if not self.measure_track(r, low_priority=True):
                        self.state["errors"] += 1
                except Exception as e:
                    self.state["errors"] += 1
                    log.warning("音量を測れませんでした: %s: %s", r["rel"], e)
                self.state["done"] += 1
        finally:
            self.state["running"] = False
            self._lock.release()
