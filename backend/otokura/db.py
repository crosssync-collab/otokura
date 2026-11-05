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
CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  pw_hash     TEXT NOT NULL,
  role        TEXT NOT NULL,             -- admin / member
  created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings (   -- 人ごとの設定
  user_id     INTEGER NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  PRIMARY KEY(user_id, key)
);
CREATE TABLE IF NOT EXISTS track_stats (     -- 人ごとの記録。スキャンで消さない(パスで結びつける)
  user_id     INTEGER NOT NULL DEFAULT 1,
  source      TEXT NOT NULL,
  rel         TEXT NOT NULL,
  plays       INTEGER NOT NULL DEFAULT 0,
  last_played REAL,
  skips       INTEGER NOT NULL DEFAULT 0,
  fav         INTEGER NOT NULL DEFAULT 0,
  rating      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, source, rel)
);
CREATE TABLE IF NOT EXISTS playlists (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL DEFAULT 1,
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
CREATE TABLE IF NOT EXISTS loudness (         -- ffmpeg で測った音量と無音(スキャンで消さない。ファイルが変わったら測り直す)
  source  TEXT NOT NULL,
  rel     TEXT NOT NULL,
  mtime   REAL NOT NULL,
  size    INTEGER NOT NULL,
  lufs    REAL,
  head_ms INTEGER,
  tail_ms INTEGER,
  PRIMARY KEY(source, rel)
);
CREATE TABLE IF NOT EXISTS artist_info (      -- Web から取ったアーティスト情報(webinfo.py)
  name         TEXT PRIMARY KEY,
  mbid         TEXT,
  image        TEXT,                       -- /data/art 内のファイル名の元
  image_source TEXT,
  summary      TEXT,
  summary_url  TEXT,
  status       TEXT NOT NULL,              -- ok / none(調べたが見つからない)
  digest       TEXT,
  fetched_at   REAL NOT NULL,              -- 最後に調べた時刻
  first_at     REAL NOT NULL,
  updated_at   REAL NOT NULL               -- 内容が最後に変わった時刻
);
CREATE TABLE IF NOT EXISTS album_info (       -- Web から取ったアルバム情報(albums.key で結びつける)
  key          TEXT PRIMARY KEY,
  mbid         TEXT,
  release_date TEXT,
  kind         TEXT,
  label        TEXT,
  summary      TEXT,
  summary_url  TEXT,
  source       TEXT,
  status       TEXT NOT NULL,
  digest       TEXT,
  fetched_at   REAL NOT NULL,
  first_at     REAL NOT NULL,
  updated_at   REAL NOT NULL
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
        self._to_accounts(c)
        c.executescript(SCHEMA)
        for col in ("cue_start INTEGER", "cue_end INTEGER",    # CUE シートの曲: 元のファイルの何ミリ秒から何ミリ秒まで
                    "rg_track REAL", "rg_album REAL"):         # ReplayGain のタグ(dB。-18 LUFS 基準)
            try:
                c.execute(f"ALTER TABLE tracks ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        c.commit()

    def _to_accounts(self, c: sqlite3.Connection) -> None:
        """アカウントの無かった頃の DB の記録を、最初の管理者(id=1)のものにする。移す前の DB を残す。"""
        cols = [r[1] for r in c.execute("PRAGMA table_info(track_stats)")]
        if not cols or "user_id" in cols:
            return
        backup = Path(self.path).with_name("otokura.pre-accounts.db")
        if not backup.exists():
            c.execute("VACUUM INTO ?", (str(backup),))
        c.executescript("""
          BEGIN;
          ALTER TABLE track_stats RENAME TO track_stats_old;
          CREATE TABLE track_stats (user_id INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL, rel TEXT NOT NULL,
            plays INTEGER NOT NULL DEFAULT 0, last_played REAL, skips INTEGER NOT NULL DEFAULT 0, fav INTEGER NOT NULL DEFAULT 0,
            rating INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, source, rel));
          INSERT INTO track_stats(user_id,source,rel,plays,last_played,skips,fav,rating)
            SELECT 1,source,rel,plays,last_played,skips,fav,rating FROM track_stats_old;
          DROP TABLE track_stats_old;
          ALTER TABLE playlists ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
          CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(user_id, key));
          INSERT OR IGNORE INTO user_settings(user_id,key,value) SELECT 1,key,value FROM settings;
          COMMIT;
        """)

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
