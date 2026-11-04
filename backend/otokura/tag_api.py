"""タグ(メタデータ)の表示・編集 API。音楽ファイルに書き、書く前の値を tag_edits に残して元に戻せるようにする。"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from . import tagedit

SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_edits (
  id         INTEGER PRIMARY KEY,
  grp        TEXT NOT NULL,             -- アルバムまとめての編集は同じ grp
  source     TEXT NOT NULL,
  rel        TEXT NOT NULL,
  at         REAL NOT NULL,
  keys       TEXT NOT NULL,             -- 書き換えたキー(表示用)
  before     TEXT NOT NULL,             -- {キー: 書く前の値のリスト or null}
  cover      TEXT,                      -- カバーを変えたとき: 前の画像のファイル名、無かったら ''
  reverted   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tag_edits_track ON tag_edits(source, rel);
"""
MAX_COVER = 20 * 1024 * 1024


def make_router(db, cfg, scanner, src_by_id, track_row, track_path) -> APIRouter:
    r = APIRouter()
    with db.write_lock:
        db.conn.executescript(SCHEMA)
    hist_dir = cfg.data_dir / "tag-history"

    def writable(p: Path) -> None:
        if not os.access(p, os.W_OK):
            raise HTTPException(409, "音楽ファイルに書き込めません。音楽フォルダが読み取り専用でつながれていないか確認してください")

    def edit_one(row, fields=None, tags=None, cover=None, remove_cover=False, grp=None) -> dict:
        if row["cue_start"] is not None:
            raise HTTPException(409, "CUE シートの曲はタグを編集できません(曲名などは .cue ファイルを書き換えてください)")
        p = track_path(row)
        writable(p)
        try:
            res = tagedit.write(p, fields, tags, cover, remove_cover)
        except tagedit.TagError as e:
            raise HTTPException(400, str(e)) from e
        cover_name = None
        if res["coverChanged"]:
            cover_name = ""
            if res["cover"]:
                hist_dir.mkdir(parents=True, exist_ok=True)
                cover_name = f"{uuid.uuid4().hex}.bin"
                (hist_dir / cover_name).write_bytes(res["cover"])
        if res["before"] or res["coverChanged"]:
            with db.write_lock:
                db.conn.execute("INSERT INTO tag_edits(grp,source,rel,at,keys,before,cover) VALUES(?,?,?,?,?,?,?)",
                                (grp or uuid.uuid4().hex, row["source"], row["rel"], time.time(),
                                 json.dumps(list(res["before"]) + (["カバー"] if res["coverChanged"] else []),
                                            ensure_ascii=False),
                                 json.dumps(res["before"], ensure_ascii=False), cover_name))
                db.conn.commit()
        return res

    def reread(rows, new_art=False) -> None:
        items = [(src_by_id[x["source"]], x["rel"]) for x in rows if x["source"] in src_by_id]
        if not scanner.update_files(items, new_art=new_art):
            raise HTTPException(503, "スキャン中のため、ライブラリへの反映は次のスキャンで行われます(ファイルには保存しました)")

    def history(source: str, rel: str) -> list[dict]:
        rows = db.q("SELECT id,grp,at,keys,reverted FROM tag_edits WHERE source=? AND rel=? ORDER BY id DESC LIMIT 10",
                    (source, rel))
        out = []
        for h in rows:
            n = db.one("SELECT COUNT(*) n FROM tag_edits WHERE grp=?", (h["grp"],))["n"]
            out.append({"id": h["id"], "at": h["at"], "keys": json.loads(h["keys"]), "tracks": n,
                        "reverted": bool(h["reverted"])})
        return out

    def album_rows(aid: int):
        rows = db.q("SELECT * FROM tracks WHERE album_id=? ORDER BY COALESCE(disc_no,1), COALESCE(track_no,9999), rel",
                    (aid,))
        if not rows:
            raise HTTPException(404, "アルバムが見つかりません。ライブラリを再読み込みしてください")
        return rows

    async def image_body(request: Request) -> bytes:
        data = await request.body()
        if not data:
            raise HTTPException(400, "画像がありません")
        if len(data) > MAX_COVER:
            raise HTTPException(413, "画像が大きすぎます(20MB まで)")
        return data

    # ---------- 1 曲 ----------
    @r.get("/tracks/{tid}/meta")
    def get_meta(tid: int):
        row = track_row(tid)
        p = track_path(row)
        try:
            tags = tagedit.read(p)
        except tagedit.TagError as e:
            tags = {"kind": None, "fields": {}, "tags": [], "hasCover": False, "error": str(e)}
        try:
            stt = p.stat()
        except OSError:
            raise HTTPException(404, "ファイルが見つかりません。再スキャンしてください")
        s = db.one("SELECT plays,last_played,skips,fav,rating FROM track_stats WHERE source=? AND rel=?",
                   (row["source"], row["rel"]))
        src = src_by_id.get(row["source"])
        return {
            **tags,
            "file": {"source": src.name if src else row["source"], "rel": row["rel"], "codec": row["codec"],
                     "sampleRate": row["sample_rate"], "bitDepth": row["bit_depth"], "kbps": row["bitrate"],
                     "durationMs": row["duration_ms"], "size": stt.st_size, "mtime": stt.st_mtime,
                     "addedAt": row["added_at"], "plays": s["plays"] if s else 0,
                     "lastPlayed": s["last_played"] if s else None, "skips": s["skips"] if s else 0,
                     "tagFixed": bool(row["tag_fixed"]), "tagGuessed": bool(row["tag_guessed"])},
            "writable": os.access(p, os.W_OK),
            "history": history(row["source"], row["rel"]),
        }

    @r.put("/tracks/{tid}/meta")
    def put_meta(tid: int, fields: dict = Body(default={}), tags: dict = Body(default={})):
        row = track_row(tid)
        edit_one(row, fields, tags)
        reread([row])
        return {"ok": True}

    @r.get("/tracks/{tid}/cover")
    def get_cover(tid: int):
        data = tagedit.get_cover(track_path(track_row(tid)))
        if not data:
            raise HTTPException(404, "埋め込みのカバー画像はありません")
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return Response(data, media_type=mime, headers={"Cache-Control": "no-cache"})

    @r.put("/tracks/{tid}/cover")
    async def put_cover(tid: int, request: Request):
        row = track_row(tid)
        edit_one(row, cover=await image_body(request))
        reread([row], new_art=True)
        return {"ok": True}

    @r.delete("/tracks/{tid}/cover")
    def delete_cover(tid: int):
        row = track_row(tid)
        edit_one(row, remove_cover=True)
        reread([row], new_art=True)
        return {"ok": True}

    # ---------- アルバムまとめて ----------
    @r.get("/albums/{aid}/meta")
    def get_album_meta(aid: int):
        rows = album_rows(aid)
        vals: dict[str, set[str]] = {f: set() for f in tagedit.ALBUM_FIELDS}
        writable_all = True
        for row in rows:
            p = track_path(row)
            writable_all &= os.access(p, os.W_OK)
            try:
                fs = tagedit.read(p)["fields"]
            except tagedit.TagError:
                continue
            for f in vals:
                vals[f].add(fs.get(f, ""))
        return {"tracks": len(rows), "writable": writable_all,
                "fields": {f: (next(iter(v)) if len(v) == 1 else None) for f, v in vals.items()},   # None は曲ごとに違う
                "history": history(rows[0]["source"], rows[0]["rel"])}

    @r.put("/albums/{aid}/meta")
    def put_album_meta(aid: int, fields: dict = Body(..., embed=True)):
        bad = set(fields) - set(tagedit.ALBUM_FIELDS)
        if bad:
            raise HTTPException(400, f"アルバムまとめては {', '.join(tagedit.ALBUM_FIELDS)} だけ書き換えられます")
        rows = album_rows(aid)
        grp = uuid.uuid4().hex
        for row in rows:
            edit_one(row, fields, grp=grp)
        reread(rows)
        return {"ok": True, "tracks": len(rows)}

    @r.put("/albums/{aid}/cover")
    async def put_album_cover(aid: int, request: Request):
        data = await image_body(request)
        rows = album_rows(aid)
        grp = uuid.uuid4().hex
        for row in rows:
            edit_one(row, cover=data, grp=grp)
        reread(rows, new_art=True)
        return {"ok": True, "tracks": len(rows)}

    # ---------- 元に戻す ----------
    @r.post("/tagedits/{eid}/revert")
    def revert(eid: int):
        h = db.one("SELECT grp FROM tag_edits WHERE id=?", (eid,))
        if h is None:
            raise HTTPException(404, "編集の記録が見つかりません")
        edits = db.q("SELECT * FROM tag_edits WHERE grp=? AND reverted=0", (h["grp"],))
        if not edits:
            raise HTTPException(409, "この編集はもう元に戻してあります")
        rows, new_art = [], False
        for e in edits:
            row = db.one("SELECT * FROM tracks WHERE source=? AND rel=?", (e["source"], e["rel"]))
            if row is None:
                continue
            cover, remove = None, False
            if e["cover"] is not None:
                new_art = True
                if e["cover"]:
                    cover = (hist_dir / e["cover"]).read_bytes()
                else:
                    remove = True
            p = track_path(row)
            writable(p)
            try:
                tagedit.write(p, tags=json.loads(e["before"]), cover=cover, remove_cover=remove)
            except tagedit.TagError as err:
                raise HTTPException(400, str(err)) from err
            with db.write_lock:
                db.conn.execute("UPDATE tag_edits SET reverted=1 WHERE id=?", (e["id"],))
                db.conn.commit()
            rows.append(row)
        reread(rows, new_art=new_art)
        return {"ok": True, "tracks": len(rows)}

    return r
