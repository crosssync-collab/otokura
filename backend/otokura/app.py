"""音蔵の Web サーバー(FastAPI)。

    uvicorn otokura.app:app --host 0.0.0.0 --port 8686
"""
from __future__ import annotations

import re

import json
import logging
import platform
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import __version__, art, metadata
from .config import Config, from_env
from .db import Database
from .dlna import Dlna, DlnaError, didl
from .loudness import REFERENCE_LUFS, Loudness
from .lyrics.api import make_router as lyrics_router
from .lyrics.folder import LyricsFolder
from .lyrics.lrclib import LrcLib
from .lyrics.store import LyricsStore, TrackRef
from .tag_api import make_router as tag_router
from .transcode import Transcoder, CODECS as TRANSCODE_CODECS, LOSSY as TRANSCODE_LOSSY, target as transcode_target
from .scanner import Scanner, Scheduler
from .webinfo import WebInfo

log = logging.getLogger("otokura")


def create_app(cfg: Config | None = None, start_background: bool = True) -> FastAPI:
    cfg = cfg or from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    lyrics = LyricsStore(cfg.db_path, lrclib=LrcLib() if cfg.lrclib else None,
                         folder=LyricsFolder(cfg.lyrics_dirs) if cfg.lyrics_dirs else None)
    scanner = Scanner(db, cfg, lyrics)
    tc = Transcoder(cfg.data_dir / "transcode", int(cfg.transcode_max_gb * 1024 ** 3))
    src_by_id = {s.id: s for s in cfg.sources}

    app = FastAPI(title="音蔵", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.state.cfg, app.state.db, app.state.scanner, app.state.lyrics, app.state.transcoder = cfg, db, scanner, lyrics, tc
    # 鳴らせる形式 = ブラウザがそのまま鳴らせるもの + ffmpeg で変換するもの
    def playable_codecs() -> set[str]:
        return metadata.BROWSER_PLAYABLE | (TRANSCODE_CODECS if tc.available else set())

    webinfo = WebInfo(db, cfg.art_dir) if cfg.webinfo else None
    dlna = Dlna(db)
    app.state.webinfo, app.state.dlna = webinfo, dlna

    def bump():
        db.meta("library_version", str(time.time()))
        if webinfo and start_background:
            webinfo.daily()
    scanner.on_done = bump

    # ---------- 共通 ----------
    def track_row(tid: int):
        r = db.one("SELECT * FROM tracks WHERE id=?", (tid,))
        if r is None:
            raise HTTPException(404, "曲が見つかりません。ライブラリを再読み込みしてください")
        return r

    def track_path(r) -> Path:
        src = src_by_id.get(r["source"])
        if src is None:
            raise HTTPException(404, "この曲のソースは設定から外されています")
        rel = r["rel"].rsplit("#", 1)[0] if "cue_start" in r.keys() and r["cue_start"] is not None else r["rel"]   # CUE の曲は「元のファイル#曲番号」
        p = (src.path / rel).resolve()
        if src.path.resolve() not in p.parents:
            raise HTTPException(400, "不正なパスです")
        return p

    def get_track_ref(tid: str) -> TrackRef | None:
        try:
            r = db.one("SELECT * FROM tracks WHERE id=?", (int(tid),))
        except ValueError:
            return None
        if r is None or r["source"] not in src_by_id:
            return None
        return TrackRef(str(r["id"]), str(src_by_id[r["source"]].path / r["rel"]), r["title"], r["artist"],
                        r["album"], r["duration_ms"])

    def stat_key(tid: int):
        r = track_row(tid)
        return r["source"], r["rel"]

    loud = Loudness(db, track_path)
    app.state.loudness = loud
    app.include_router(tag_router(db, cfg, scanner, src_by_id, track_row, track_path), prefix="/api")

    def ensure_stat(source: str, rel: str):
        db.conn.execute("INSERT OR IGNORE INTO track_stats(source,rel) VALUES(?,?)", (source, rel))

    # ---------- ライブラリ ----------
    @app.get("/api/library")
    def library(request: Request):
        """画面が起動時に読む一覧。配列は先頭の columns の順。"""
        ver = db.meta("library_version") or "0"
        albums = db.q("SELECT id,title,artist,year,genre,source,folder,art IS NOT NULL AS has_art,hue,added_at,art "
                      "FROM albums ORDER BY id")
        tracks = db.q(
            "SELECT t.id,t.album_id,t.disc_no,t.track_no,t.title,t.artist,t.duration_ms,t.codec,t.sample_rate,"
            "t.bit_depth,t.bitrate,t.rel,t.source,COALESCE(s.plays,0) plays,s.last_played,COALESCE(s.skips,0) skips,"
            "COALESCE(s.fav,0) fav,COALESCE(s.rating,0) rating,t.tag_fixed,t.tag_guessed,t.cue_start IS NOT NULL cue,"
            f"ROUND(COALESCE(t.rg_track,{REFERENCE_LUFS}-l.lufs),2) rg,t.rg_album,l.head_ms,l.tail_ms "
            "FROM tracks t LEFT JOIN track_stats s ON s.source=t.source AND s.rel=t.rel "
            "LEFT JOIN loudness l ON l.source=t.source AND l.rel=t.rel AND l.mtime=t.mtime AND l.size=t.size "
            "ORDER BY t.album_id, COALESCE(t.disc_no,1), COALESCE(t.track_no,9999), t.rel")
        return {
            "version": ver,
            "now": time.time(),
            "sources": [{"id": s.id, "name": s.name, "path": str(s.path)} for s in cfg.sources],
            "playable": sorted(playable_codecs()),
            "transcode": {c: transcode_target(c)[0] for c in sorted(TRANSCODE_CODECS)} if tc.available else {},
            "albumColumns": ["id", "title", "artist", "year", "genre", "source", "folder", "hasArt", "hue", "addedAt", "art"],
            "albums": [list(r) for r in albums],
            "trackColumns": ["id", "albumId", "disc", "no", "title", "artist", "durationMs", "codec", "sampleRate",
                             "bitDepth", "kbps", "rel", "source", "plays", "lastPlayed", "skips", "fav", "rating",
                             "tagFixed", "tagGuessed", "cue", "rgTrack", "rgAlbum", "headMs", "tailMs"],
            "tracks": [list(r) for r in tracks],
            "artistArt": {r["name"]: r["image"] for r in db.q("SELECT name, image FROM artist_info WHERE image IS NOT NULL")},
        }

    def stream_source(r, direct: bool = False, cast: bool = False) -> tuple[Path, str]:
        """配るファイルと MIME。変換が要るものは変換してから返す。"""
        p = track_path(r)
        if not p.is_file():
            raise HTTPException(404, "ファイルが見つかりません。再スキャンしてください")
        codec = r["codec"]
        if r["cue_start"] is not None:      # CUE シート・チャプターの曲: その区間だけを切り出して配る
            if not tc.available:
                raise HTTPException(415, "CUE シートの曲の再生には ffmpeg が必要です")
            try:
                out = tc.get(p, codec, cast=cast and (r["sample_rate"] or 0) > 48000 and codec not in TRANSCODE_LOSSY,
                             cut=(r["cue_start"], r["cue_end"]))
            except Exception as e:
                raise HTTPException(500, f"曲を切り出せませんでした: {e}")
            return out, transcode_target(codec)[2]
        hires = cast and codec not in TRANSCODE_LOSSY and ((r["sample_rate"] or 0) > 48000 or codec == "DSD")
        if hires and tc.available:
            try:
                return tc.get(p, codec, cast=True), "audio/flac"
            except Exception as e:
                raise HTTPException(500, f"Cast 向けに変換できませんでした: {e}")
        if codec in metadata.BROWSER_PLAYABLE or (direct and codec in metadata.MIME):
            return p, metadata.MIME.get(codec, "application/octet-stream")
        if codec not in TRANSCODE_CODECS or not tc.available:
            raise HTTPException(415, f"{codec} は再生できません(変換に使う ffmpeg がありません)")
        try:
            return tc.get(p, codec), transcode_target(codec)[2]
        except Exception as e:
            raise HTTPException(500, f"{codec} を変換できませんでした: {e}")

    @app.api_route("/api/tracks/{tid}/stream", methods=["GET", "HEAD"])     # DLNA の機器は先に HEAD で調べることがある
    def stream(tid: int, direct: bool = False, cast: bool = False, dlna: bool = False):
        """曲を配る。ブラウザが鳴らせない形式は変換したものを配る(direct=1 は、その端末がそのまま鳴らせると言うとき。Safari の ALAC など)。
        cast=1 は Chromecast・DLNA 向け: 48kHz を超える曲(ハイレゾ・DSD)は 24bit・48kHz の FLAC にする。dlna=1 は DLNA の機器が見る応答ヘッダーを付ける。"""
        path, mime = stream_source(track_row(tid), direct, cast)
        headers = {"Cache-Control": "private, max-age=3600"}
        if dlna:
            headers.update({"transferMode.dlna.org": "Streaming",
                            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"})
        return FileResponse(path, media_type=mime, headers=headers)

    @app.post("/api/tracks/{tid}/loudness")
    def track_loudness(tid: int):
        """この曲の音量と頭・お尻の無音。測っていなければ測る(初回は数秒〜十数秒かかる)。"""
        r = track_row(tid)
        res = loud.measure_track(r)
        gain = r["rg_track"] if r["rg_track"] is not None else (round(REFERENCE_LUFS - res["lufs"], 2) if res else None)
        return {"rgTrack": gain, "rgAlbum": r["rg_album"], "headMs": res["head_ms"] if res else None,
                "tailMs": res["tail_ms"] if res else None}

    @app.get("/api/loudness")
    def loudness_state():
        return loud.public()

    @app.post("/api/loudness/start")
    def loudness_start():
        if not tc.available:
            raise HTTPException(409, "音量を測るには ffmpeg が必要です")
        return {"started": loud.start(), **loud.public()}

    @app.post("/api/loudness/stop")
    def loudness_stop():
        loud.stop()
        return loud.public()

    @app.post("/api/tracks/{tid}/prepare")
    def prepare(tid: int):
        """次の曲が変換の要る形式なら、先に変換を始めておく(曲の切り替えで待たないように)。"""
        r = track_row(tid)
        codec = r["codec"]
        cut = (r["cue_start"], r["cue_end"]) if r["cue_start"] is not None else None
        if not cut and (codec in metadata.BROWSER_PLAYABLE or codec not in TRANSCODE_CODECS) or not tc.available:
            return {"needed": False, "ready": True}
        p = track_path(r)
        if not p.is_file():
            raise HTTPException(404, "ファイルが見つかりません")
        ready = tc.ready(p, codec, cut=cut)
        if not ready:
            tc.prepare(p, codec, cut=cut)
        return {"needed": True, "ready": ready}

    @app.get("/api/art/{album_id}")
    def album_art(album_id: int, size: int = 300):
        r = db.one("SELECT art FROM albums WHERE id=?", (album_id,))
        if r is None or not r["art"]:
            raise HTTPException(404, "カバー画像がありません")
        p = art.thumb_path(cfg.art_dir, r["art"], size)
        if not p.is_file():
            raise HTTPException(404, "カバー画像がありません")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    # ---------- Web から取るアーティスト画像・アルバム情報 ----------
    def need_webinfo() -> WebInfo:
        if webinfo is None:
            raise HTTPException(409, "Web からの取得は無効になっています(OTOKURA_WEBINFO)")
        return webinfo

    def artist_out(name: str, row) -> dict:
        if row is None:
            return {"name": name, "status": "missing"}
        return {"name": name, "status": row["status"], "image": row["image"], "imageSource": row["image_source"],
                "summary": row["summary"], "summaryUrl": row["summary_url"], "mbid": row["mbid"],
                "fetchedAt": row["fetched_at"], "updatedAt": row["updated_at"]}

    def album_out(row) -> dict:
        if row is None:
            return {"status": "missing"}
        return {"status": row["status"], "releaseDate": row["release_date"], "kind": row["kind"], "label": row["label"],
                "summary": row["summary"], "summaryUrl": row["summary_url"], "source": row["source"], "mbid": row["mbid"],
                "fetchedAt": row["fetched_at"], "updatedAt": row["updated_at"]}

    @app.get("/api/artists/info")
    def artist_info(name: str, fetch: bool = False):
        """fetch=1: まだ調べていなければ順番待ちの先頭に入れる。"""
        row = db.one("SELECT * FROM artist_info WHERE name=?", (name,))
        if row is None and fetch and webinfo and db.one("SELECT 1 FROM albums WHERE artist=? LIMIT 1", (name,)):
            webinfo.enqueue("artist", name, first=True)
            return {"name": name, "status": "pending"}
        return artist_out(name, row)

    @app.post("/api/artists/refresh")
    def artist_refresh(body: dict = Body(...)):
        name = str(body.get("name") or "")
        if not db.one("SELECT 1 FROM albums WHERE artist=? LIMIT 1", (name,)):
            raise HTTPException(404, "アーティストが見つかりません")
        need_webinfo().enqueue("artist", name, first=True)
        return {"name": name, "status": "pending"}

    @app.get("/api/artistart/{image}")
    def artist_art(image: str, size: int = 300):
        p = art.thumb_path(cfg.art_dir, image, size) if re.fullmatch(r"[0-9a-f]{20}", image) else None
        if not p or not p.is_file():
            raise HTTPException(404, "画像がありません")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/albums/{album_id}/info")
    def album_info(album_id: int, fetch: bool = False):
        al = db.one("SELECT key FROM albums WHERE id=?", (album_id,))
        if al is None:
            raise HTTPException(404, "アルバムが見つかりません")
        row = db.one("SELECT * FROM album_info WHERE key=?", (al["key"],))
        if row is None and fetch and webinfo:
            webinfo.enqueue("album", al["key"], first=True)
            return {"status": "pending"}
        return album_out(row)

    @app.post("/api/albums/{album_id}/info/refresh")
    def album_refresh(album_id: int):
        al = db.one("SELECT key FROM albums WHERE id=?", (album_id,))
        if al is None:
            raise HTTPException(404, "アルバムが見つかりません")
        need_webinfo().enqueue("album", al["key"], first=True)
        return {"status": "pending"}

    @app.get("/api/webinfo")
    def webinfo_state():
        return {"enabled": bool(webinfo), **(webinfo.public() if webinfo else {})}

    @app.post("/api/webinfo/start")
    def webinfo_start():
        """まだ調べていないものをすべて取りに行く。"""
        return {"queuedNow": need_webinfo().enqueue_missing(), **webinfo.public()}

    @app.post("/api/webinfo/check")
    def webinfo_check(days: float = 30):
        """調べてから days 日以上経ったものを調べ直す(更新の確認)。"""
        return {"queuedNow": need_webinfo().enqueue_stale(max(0, days)), **webinfo.public()}

    @app.post("/api/webinfo/stop")
    def webinfo_stop():
        need_webinfo().stop()
        return webinfo.public()

    # ---------- DLNA・UPnP ----------
    def dlna_call(fn):
        try:
            return fn()
        except DlnaError as e:
            raise HTTPException(502, str(e))

    @app.get("/api/dlna/devices")
    def dlna_devices(search: bool = False):
        """search=1 で探し直す(2〜3 秒かかる)。"""
        if search or dlna.searched_at is None:
            dlna.search()
        return {"devices": [d.public() for d in dlna.devices.values()], "castUrl": cfg.cast_url}

    @app.post("/api/dlna/devices")
    def dlna_add(body: dict = Body(...)):
        try:
            return dlna.add(str(body.get("address") or "")).public()
        except DlnaError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/dlna/devices/{did}")
    def dlna_remove(did: str):
        dlna.remove(did)
        return {"ok": True}

    @app.post("/api/dlna/{did}/load")
    def dlna_load(did: str, body: dict = Body(...)):
        if not cfg.cast_url:
            raise HTTPException(409, "機器が曲を取りに来るアドレスがありません。.env の OTOKURA_CAST_URL に NAS の家の中のアドレスを書いてください")
        r = track_row(int(body["trackId"]))
        _, mime = stream_source(r, cast=True)        # 変換が要る曲は、機器に渡す前に変換しておく
        url = f"{cfg.cast_url}/api/tracks/{r['id']}/stream?cast=1&dlna=1"
        al = db.one("SELECT art FROM albums WHERE id=?", (r["album_id"],))
        cover = f"{cfg.cast_url}/api/art/{r['album_id']}?size=300" if al and al["art"] else None
        meta = didl(url, mime, r["title"], r["artist"], r["album"], cover, r["duration_ms"])
        dlna_call(lambda: dlna.get(did).load(url, meta, float(body.get("pos") or 0), body.get("play", True) is not False))
        return {"uri": url, "mime": mime}

    @app.post("/api/dlna/{did}/play")
    def dlna_play(did: str):
        dlna_call(lambda: dlna.get(did).play())
        return {"ok": True}

    @app.post("/api/dlna/{did}/pause")
    def dlna_pause(did: str):
        dlna_call(lambda: dlna.get(did).pause())
        return {"ok": True}

    @app.post("/api/dlna/{did}/stop")
    def dlna_stop(did: str):
        dlna_call(lambda: dlna.get(did).stop())
        return {"ok": True}

    @app.post("/api/dlna/{did}/seek")
    def dlna_seek(did: str, body: dict = Body(...)):
        dlna_call(lambda: dlna.get(did).seek(float(body.get("pos") or 0)))
        return {"ok": True}

    @app.post("/api/dlna/{did}/volume")
    def dlna_volume(did: str, body: dict = Body(...)):
        dlna_call(lambda: dlna.get(did).set_volume(int(body.get("volume") or 0)))
        return {"ok": True}

    @app.get("/api/dlna/{did}/status")
    def dlna_status(did: str):
        return dlna_call(lambda: dlna.get(did).status())

    # ---------- 再生記録・お気に入り・評価 ----------
    @app.post("/api/tracks/{tid}/played")
    def played(tid: int):
        s, rel = stat_key(tid)
        with db.write_lock:
            ensure_stat(s, rel)
            db.conn.execute("UPDATE track_stats SET plays=plays+1, last_played=? WHERE source=? AND rel=?",
                            (time.time(), s, rel))
            db.conn.commit()
        return stats(tid)

    @app.post("/api/tracks/{tid}/skipped")
    def skipped(tid: int):
        s, rel = stat_key(tid)
        with db.write_lock:
            ensure_stat(s, rel)
            db.conn.execute("UPDATE track_stats SET skips=skips+1 WHERE source=? AND rel=?", (s, rel))
            db.conn.commit()
        return stats(tid)

    @app.put("/api/tracks/{tid}/stats")
    def put_stats(tid: int, body: dict = Body(...)):
        s, rel = stat_key(tid)
        with db.write_lock:
            ensure_stat(s, rel)
            if "fav" in body:
                db.conn.execute("UPDATE track_stats SET fav=? WHERE source=? AND rel=?", (int(bool(body["fav"])), s, rel))
            if "rating" in body:
                v = max(0, min(5, int(body["rating"])))
                db.conn.execute("UPDATE track_stats SET rating=? WHERE source=? AND rel=?", (v, s, rel))
            db.conn.commit()
        return stats(tid)

    @app.get("/api/tracks/{tid}/stats")
    def stats(tid: int):
        s, rel = stat_key(tid)
        r = db.one("SELECT * FROM track_stats WHERE source=? AND rel=?", (s, rel))
        return {"id": tid, "plays": r["plays"] if r else 0, "lastPlayed": r["last_played"] if r else None,
                "skips": r["skips"] if r else 0, "fav": bool(r and r["fav"]), "rating": r["rating"] if r else 0}

    # ---------- プレイリスト ----------
    def pl_out(p) -> dict:
        items = db.q("SELECT t.id FROM playlist_items i JOIN tracks t ON t.source=i.source AND t.rel=i.rel "
                     "WHERE i.playlist_id=? ORDER BY i.pos", (p["id"],))
        missing = db.one("SELECT COUNT(*) n FROM playlist_items i LEFT JOIN tracks t ON t.source=i.source AND "
                         "t.rel=i.rel WHERE i.playlist_id=? AND t.id IS NULL", (p["id"],))["n"]
        return {"id": p["id"], "name": p["name"], "rule": json.loads(p["rule"]) if p["rule"] else None,
                "trackIds": [r["id"] for r in items], "missing": missing, "updatedAt": p["updated_at"]}

    def set_items(pid: int, ids: list[int]):
        c = db.conn
        c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
        pos = 0
        for tid in ids:
            r = db.one("SELECT source, rel FROM tracks WHERE id=?", (int(tid),))
            if r:
                c.execute("INSERT INTO playlist_items(playlist_id,pos,source,rel) VALUES(?,?,?,?)",
                          (pid, pos, r["source"], r["rel"]))
                pos += 1

    @app.get("/api/playlists")
    def playlists():
        return [pl_out(p) for p in db.q("SELECT * FROM playlists ORDER BY name COLLATE NOCASE")]

    @app.post("/api/playlists")
    def create_playlist(body: dict = Body(...)):
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(422, "プレイリストの名前を入力してください")
        rule = body.get("rule")
        with db.write_lock:
            now = time.time()
            cur = db.conn.execute("INSERT INTO playlists(name,rule,created_at,updated_at) VALUES(?,?,?,?)",
                                  (name, json.dumps(rule, ensure_ascii=False) if rule else None, now, now))
            set_items(cur.lastrowid, body.get("trackIds") or [])
            db.conn.commit()
        return pl_out(db.one("SELECT * FROM playlists WHERE id=?", (cur.lastrowid,)))

    @app.put("/api/playlists/{pid}")
    def update_playlist(pid: int, body: dict = Body(...)):
        p = db.one("SELECT * FROM playlists WHERE id=?", (pid,))
        if p is None:
            raise HTTPException(404, "プレイリストが見つかりません")
        with db.write_lock:
            if "name" in body and str(body["name"]).strip():
                db.conn.execute("UPDATE playlists SET name=? WHERE id=?", (str(body["name"]).strip(), pid))
            if "rule" in body:
                db.conn.execute("UPDATE playlists SET rule=? WHERE id=?",
                                (json.dumps(body["rule"], ensure_ascii=False) if body["rule"] else None, pid))
            if "trackIds" in body:
                set_items(pid, body["trackIds"])
            if "addTrackIds" in body:
                cur_ids = pl_out(p)["trackIds"]
                set_items(pid, cur_ids + list(body["addTrackIds"]))
            db.conn.execute("UPDATE playlists SET updated_at=? WHERE id=?", (time.time(), pid))
            db.conn.commit()
        return pl_out(db.one("SELECT * FROM playlists WHERE id=?", (pid,)))

    @app.delete("/api/playlists/{pid}")
    def delete_playlist(pid: int):
        with db.write_lock:
            db.conn.execute("DELETE FROM playlists WHERE id=?", (pid,))
            db.conn.commit()
        return {"ok": True}

    # ---------- 設定 ----------
    @app.get("/api/settings")
    def get_settings():
        return {r["key"]: json.loads(r["value"]) for r in db.q("SELECT key, value FROM settings")}

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        with db.write_lock:
            for k, v in body.items():
                db.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (str(k), json.dumps(v, ensure_ascii=False)))
            db.conn.commit()
        return get_settings()

    # ---------- サーバー・スキャン ----------
    @app.get("/api/server")
    def server():
        counts = {r["source"]: r["n"] for r in db.q("SELECT source, COUNT(*) n FROM tracks GROUP BY source")}
        last = db.meta("last_scan")
        unplayable = db.q("SELECT codec, COUNT(*) n FROM tracks GROUP BY codec ORDER BY n DESC")
        return {
            "version": __version__,
            "python": platform.python_version(),
            "sources": [{"id": s.id, "name": s.name, "path": str(s.path), "ok": s.path.is_dir(),
                         "tracks": counts.get(s.id, 0)} for s in cfg.sources],
            "lastScan": float(last) if last else None,
            "scanAt": cfg.scan_at,
            "scan": scanner.state.public(),
            "codecs": [{"codec": r["codec"], "tracks": r["n"], "playable": r["codec"] in playable_codecs(),
                        "transcode": transcode_target(r["codec"])[0]
                        if r["codec"] in TRANSCODE_CODECS and r["codec"] not in metadata.BROWSER_PLAYABLE else None}
                       for r in unplayable],
            "ffmpeg": tc.available,
            "lrclib": cfg.lrclib,
            "castUrl": cfg.cast_url,
            "webinfo": bool(webinfo),
            "dataDir": str(cfg.data_dir),
        }

    @app.get("/api/scan")
    def scan_state():
        return scanner.state.public()

    @app.post("/api/scan")
    def scan(full: bool = False):
        started = scanner.start(full=full)
        return {"started": started, **scanner.state.public()}

    app.include_router(lyrics_router(lyrics, get_track_ref), prefix="/api")

    # ---------- 画面 ----------
    @app.get("/", response_class=HTMLResponse)
    def index():
        if not cfg.web_dir or not (cfg.web_dir / "index.html").is_file():
            return HTMLResponse("<p>web/index.html が見つかりません</p>", status_code=500)
        html = (cfg.web_dir / "index.html").read_text(encoding="utf-8")
        inject = "<script>window.OTOKURA_API='';</script>"
        html = html.replace("<head>", "<head>" + inject, 1) if "<head>" in html else inject + html
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ホーム画面に追加するためのファイル(PWA)。決まった名前だけ配る
    web_files = {"manifest.webmanifest": "application/manifest+json", "sw.js": "text/javascript",
                 "icon.svg": "image/svg+xml", "icon-192.png": "image/png", "icon-512.png": "image/png",
                 "icon-180.png": "image/png"}

    # ミックスのジャケット(web/mix/*.jpg)
    @app.get("/mix/{name}", include_in_schema=False)
    def mix_art(name: str):
        p = cfg.web_dir / "mix" / name if cfg.web_dir and re.fullmatch(r"[a-z0-9]+\.jpg", name) else None
        if not p or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @app.get("/{name}", include_in_schema=False)
    def web_file(name: str):
        p = cfg.web_dir / name if cfg.web_dir and name in web_files else None
        if not p or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type=web_files[name], headers={"Cache-Control": "no-cache"})

    if start_background:
        @app.on_event("startup")
        def _start():
            if cfg.scan_on_start:
                scanner.start()
            if cfg.scan_at:
                Scheduler(scanner, cfg.scan_at).start()

    return app


def _lazy_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


_app = None


def __getattr__(name):     # `uvicorn otokura.app:app` で初めて作る(テストで import しても起動しない)
    global _app
    if name == "app":
        if _app is None:
            _app = _lazy_app()
        return _app
    raise AttributeError(name)
