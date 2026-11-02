"""ライブラリの SQLite。スキャナとAPIのスレッドから使うので、接続はスレッドごとに作る。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS albums (
  id          INTEGER PRIMARY KEY,
  key         TEXT NOT NULL UNIQUE,      -- source|albumartist|album(無ければ source|folder)
  title       TEXT NOT NULL,
  artist      TEXT NOT NULL,             -- アルバムアーティスト
  year        INTEGER,
  genre       TEXT,
  source      TEXT NOT NULL,
  folder      TEXT NOT NULL,             -- ソース内の相対フォルダ(代表)
  art         TEXT,                      -- /data/art 内のファイル名の元(拡張子なし)。無ければ NULL
  art_src     TEXT,                      -- 'embedded:<track path>' / 'file:<path>'
  hue         INTEGER,                   -- カバーの主な色相(0-359)
  added_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  rel         TEXT NOT NULL,             -- ソース内の相対パス
  mtime       REAL NOT NULL,
  size        INTEGER NOT NULL,
  album_id    INTEGER REFERENCES albums(id),
  title       TEXT NOT NULL,
  artist      TEXT NOT NULL,
  albumartist TEXT,
  album       TEXT,
  year        INTEGER,
  genre       TEXT,
  track_no    INTEGER,
  disc_no     INTEGER,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  codec       TEXT,                      -- FLAC / MP3 / AAC / ALAC / APE / TTA / ...
  sample_rate INTEGER,
  bit_depth   INTEGER,
  bitrate     INTEGER,
  tag_fixed   INTEGER NOT NULL DEFAULT 0, -- 文字化けを直した
  tag_guessed INTEGER NOT NULL DEFAULT 0, -- タグが無くファイル名から補った
  added_at    REAL NOT NULL,
  UNIQUE(source, rel)
);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album_id);
CREATE TABLE IF NOT EXISTS track_stats (     -- スキャンで消さない(パスで結びつける)
  source      TEXT NOT NULL,
  rel         TEXT NOT NULL,
  plays       INTEGER NOT NULL DEFAULT 0,
  last_played REAL,
  skips       INTEGER NOT NULL DEFAULT 0,
  fav         INTEGER NOT NULL DEFAULT 0,
  rating      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(source, rel)
);
CREATE TABLE IF NOT EXISTS playlists (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  rule        TEXT,                      -- スマートプレイリストの条件(JSON)。通常は NULL
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  pos         INTEGER NOT NULL,
  source      TEXT NOT NULL,
  rel         TEXT NOT NULL,
  PRIMARY KEY(playlist_id, pos)
);
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.write_lock = threading.RLock()
        c = self.conn
        c.executescript(SCHEMA)
        c.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is None:
            r = self.one("SELECT value FROM meta WHERE key=?", (key,))
            return r["value"] if r else None
        with self.write_lock:
            self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, value))
            self.conn.commit()
        return value
