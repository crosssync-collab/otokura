"""歌詞の保存(SQLite)。

- lyrics_local : スキャンで手元(サイドカー/タグ、無ければ歌詞フォルダ)から読んだ結果。ファイルの更新時刻で差分更新
- lyrics_user  : 利用者の手動登録・LRCLIB 取得結果・ずれ補正(音楽ファイルには書き込まない)
表示に使う歌詞の優先順位: 手動 > 手元 > LRCLIB
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .encoding import decode_bytes
from .folder import LyricsFolder
from .lrc import looks_like_lrc, parse_lrc, parse_plain
from .lrclib import LrcLib, Query
from .model import Lyrics
from .resolver import find_sidecars, resolve_local
from .ttml import parse_ttml

SCHEMA = """
CREATE TABLE IF NOT EXISTS lyrics_local (
  track_id   TEXT PRIMARY KEY,
  signature  TEXT NOT NULL,          -- 音楽ファイルとサイドカーの mtime。変わったら読み直す
  source     TEXT,
  synced     INTEGER NOT NULL DEFAULT 0,
  has_words  INTEGER NOT NULL DEFAULT 0,
  lang       TEXT,
  body       TEXT,                   -- Lyrics.to_json()。見つからなければ NULL
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lyrics_user (
  track_id          TEXT PRIMARY KEY,
  offset_ms         INTEGER NOT NULL DEFAULT 0,
  manual_body       TEXT,
  lrclib_body       TEXT,
  lrclib_checked_at REAL,
  lrclib_miss       INTEGER NOT NULL DEFAULT 0
);
"""

LRCLIB_RETRY_S = 30 * 86400   # 見つからなかった曲は 30 日は再問い合わせしない


@dataclass
class TrackRef:
    id: str
    path: str
    title: str
    artist: str
    album: str | None = None
    duration_ms: int | None = None


def _signature(audio: Path, folder_files: list[Path] = ()) -> str:
    parts = []
    for p in [audio, *sorted(find_sidecars(audio).values())]:
        try:
            parts.append([p.name, round(p.stat().st_mtime, 3)])
        except OSError:
            parts.append([p.name, None])
    for p in folder_files:     # 歌詞フォルダで見つかったファイル(増えた・変わったら読み直す)
        try:
            parts.append([str(p), round(p.stat().st_mtime, 3)])
        except OSError:
            parts.append([str(p), None])
    return json.dumps(parts, ensure_ascii=False)


def parse_any(text: str, source: str = "manual", track_ms: int | None = None) -> Lyrics:
    t = text.lstrip()
    if t.startswith("<?xml") or t.startswith("<tt"):
        ly = parse_ttml(text, source=source, track_ms=track_ms)
    elif looks_like_lrc(text):
        ly = parse_lrc(text, source=source, track_ms=track_ms)
    else:
        ly = parse_plain(text, source=source)
    ly.source = source
    return ly


def _locked(fn):
    """スキャンのスレッドと API のスレッドから同じ接続を使うので、1 つずつ実行する。"""
    @functools.wraps(fn)
    def wrap(self, *a, **k):
        with self.lock:
            return fn(self, *a, **k)
    return wrap


class LyricsStore:
    def __init__(self, db_path: str | Path, lrclib: LrcLib | None = None, folder: LyricsFolder | None = None):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lrclib = lrclib
        self.folder = folder

    # ---------- スキャン ----------
    @_locked
    def refresh(self, t: TrackRef, force: bool = False) -> bool:
        """手元の歌詞を必要なら読み直す。読み直したら True。"""
        audio = Path(t.path)
        folder_files = self.folder.find(t.artist, t.title) if self.folder else []
        sig = _signature(audio, folder_files)
        row = self.db.execute("SELECT signature FROM lyrics_local WHERE track_id=?", (t.id,)).fetchone()
        if row and row["signature"] == sig and not force:
            return False
        ly, _ = resolve_local(audio, t.duration_ms)
        if ly is None and folder_files:   # 横にもタグにも無いときだけ、歌詞フォルダのものを使う
            ly = self.folder.load(folder_files, t.title, t.duration_ms)
        self.db.execute(
            "INSERT INTO lyrics_local(track_id,signature,source,synced,has_words,lang,body,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(track_id) DO UPDATE SET signature=excluded.signature,"
            "source=excluded.source,synced=excluded.synced,has_words=excluded.has_words,lang=excluded.lang,"
            "body=excluded.body,updated_at=excluded.updated_at",
            (t.id, sig, ly.source if ly else None, int(bool(ly and ly.synced)), int(bool(ly and ly.has_words)),
             ly.main.lang if ly and ly.main else None,
             json.dumps(ly.to_json(), ensure_ascii=False) if ly else None, time.time()))
        self.db.commit()
        return True

    # ---------- 取得 ----------
    def _user(self, track_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM lyrics_user WHERE track_id=?", (track_id,)).fetchone()

    def _ensure_user(self, track_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO lyrics_user(track_id) VALUES(?)", (track_id,))

    @_locked
    def get(self, track_id: str) -> dict:
        u = self._user(track_id)
        local = self.db.execute("SELECT body FROM lyrics_local WHERE track_id=?", (track_id,)).fetchone()
        body = None
        if u and u["manual_body"]:
            body = u["manual_body"]
        elif local and local["body"]:
            body = local["body"]
        elif u and u["lrclib_body"]:
            body = u["lrclib_body"]
        out = json.loads(body) if body else None
        return {
            "trackId": track_id,
            "available": bool(out),
            "lyrics": out,
            "offsetMs": u["offset_ms"] if u else 0,
            "canFetch": self.lrclib is not None,
            "lrclibMiss": bool(u and u["lrclib_miss"]),
        }

    # ---------- 利用者の操作 ----------
    @_locked
    def set_offset(self, track_id: str, offset_ms: int) -> None:
        offset_ms = max(-10000, min(10000, int(offset_ms)))
        self._ensure_user(track_id)
        self.db.execute("UPDATE lyrics_user SET offset_ms=? WHERE track_id=?", (offset_ms, track_id))
        self.db.commit()

    @_locked
    def set_manual(self, track_id: str, text: str | bytes, duration_ms: int | None = None) -> Lyrics:
        if isinstance(text, bytes):
            text = decode_bytes(text)
        ly = parse_any(text, source="manual", track_ms=duration_ms)
        self._ensure_user(track_id)
        self.db.execute("UPDATE lyrics_user SET manual_body=? WHERE track_id=?",
                        (json.dumps(ly.to_json(), ensure_ascii=False), track_id))
        self.db.commit()
        return ly

    @_locked
    def clear_manual(self, track_id: str) -> None:
        self.db.execute("UPDATE lyrics_user SET manual_body=NULL WHERE track_id=?", (track_id,))
        self.db.commit()

    def fetch_lrclib(self, t: TrackRef, force: bool = False) -> Lyrics | None:
        """手元に無い曲だけ LRCLIB から取る。force でない限り、見つからなかった曲は 30 日待つ。"""
        if self.lrclib is None:
            return None
        with self.lock:
            u = self._user(t.id)
        if not force and u and u["lrclib_miss"] and u["lrclib_checked_at"] and \
                time.time() - u["lrclib_checked_at"] < LRCLIB_RETRY_S:
            return None
        ly = self.lrclib.fetch(Query(t.title, t.artist, t.album, t.duration_ms))
        with self.lock:
            self._ensure_user(t.id)
            self.db.execute(
                "UPDATE lyrics_user SET lrclib_body=?, lrclib_checked_at=?, lrclib_miss=? WHERE track_id=?",
                (json.dumps(ly.to_json(), ensure_ascii=False) if ly else None, time.time(), int(ly is None), t.id))
            self.db.commit()
        return ly

    @_locked
    def missing(self, limit: int = 100) -> list[str]:
        """手元にも LRCLIB にも歌詞が無く、まだ問い合わせていない曲(夜間の一括取得用)。"""
        rows = self.db.execute(
            "SELECT l.track_id FROM lyrics_local l LEFT JOIN lyrics_user u USING(track_id) "
            "WHERE l.body IS NULL AND (u.track_id IS NULL OR (u.lrclib_body IS NULL AND u.manual_body IS NULL "
            "AND (u.lrclib_checked_at IS NULL OR u.lrclib_checked_at < ?))) LIMIT ?",
            (time.time() - LRCLIB_RETRY_S, limit)).fetchall()
        return [r["track_id"] for r in rows]
