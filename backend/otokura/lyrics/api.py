"""歌詞の HTTP API(FastAPI ルーター)。

    app.include_router(make_router(store, get_track), prefix="/api")

get_track(track_id) -> TrackRef | None は本体のライブラリ DB から曲情報を引く関数を渡す。
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request, UploadFile

from .store import LyricsStore, TrackRef


def make_router(store: LyricsStore, get_track: Callable[[str], TrackRef | None]) -> APIRouter:
    r = APIRouter(tags=["lyrics"])

    def member(request: Request) -> int | None:
        """家族なら user id。家族の編集はその人だけのものにし、全員の既定には触らせない。"""
        u = getattr(request.state, "user", None)
        return u["id"] if u and u["role"] != "admin" else None

    def out(tid: str, uid: int | None) -> dict:
        return store.get_for(tid, uid) if uid is not None else store.get(tid)

    def track(tid: str) -> TrackRef:
        t = get_track(tid)
        if t is None:
            raise HTTPException(404, "曲が見つかりません")
        return t

    @r.get("/tracks/{tid}/lyrics")
    def get_lyrics(tid: str, request: Request):
        """表示用の歌詞。layers[].lines[] に start/end(ms)、text、words、agent、bg が入る。"""
        t = track(tid)
        store.refresh(t)               # サイドカーが更新されていれば読み直す(変化なしなら何もしない)
        return out(tid, member(request))

    @r.put("/tracks/{tid}/lyrics/offset")
    def put_offset(tid: str, request: Request, offsetMs: int = Body(..., embed=True)):
        track(tid)
        uid = member(request)
        if uid is not None:
            store.set_personal_offset(uid, tid, offsetMs)
        else:
            store.set_offset(tid, offsetMs)
        return out(tid, uid)

    @r.post("/tracks/{tid}/lyrics/fetch")
    def fetch(tid: str, request: Request):
        """LRCLIB から取得。利用者が押したときは、前回見つからなかった曲でも取りに行く。"""
        t = track(tid)
        if store.lrclib is None:
            raise HTTPException(409, "外部取得は無効になっています")
        from .lrclib import RateLimited
        try:
            store.fetch_lrclib(t, force=True)
        except RateLimited as e:
            raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.retry_after))})
        except Exception as e:   # ネットワーク障害など
            raise HTTPException(502, f"LRCLIB に接続できませんでした: {e}")
        return out(tid, member(request))

    @r.put("/tracks/{tid}/lyrics")
    def put_manual(tid: str, request: Request, text: str = Body(..., embed=True), offsetMs: int | None = Body(None, embed=True)):
        """貼り付けた歌詞(LRC / TTML / ただのテキストを自動判定)。offsetMs を渡すとずれも一緒に変える(タイミングの記録)。"""
        t = track(tid)
        if len(text) > 2_000_000:
            raise HTTPException(413, "歌詞が大きすぎます")
        uid = member(request)
        if uid is not None:
            store.set_personal(uid, tid, text, t.duration_ms, offsetMs)
        else:
            store.set_manual(tid, text, t.duration_ms, offsetMs)
        return out(tid, uid)

    @r.post("/tracks/{tid}/lyrics/upload")
    async def upload(tid: str, file: UploadFile):
        t = track(tid)
        data = await file.read()
        if len(data) > 2_000_000:
            raise HTTPException(413, "歌詞ファイルが大きすぎます")
        store.set_manual(tid, data, t.duration_ms)
        return store.get(tid)

    @r.delete("/tracks/{tid}/lyrics/manual")
    def delete_manual(tid: str, request: Request):
        track(tid)
        uid = member(request)
        if uid is not None:
            store.clear_personal(uid, tid)
        else:
            store.clear_manual(tid)
        return out(tid, uid)

    @r.post("/tracks/{tid}/lyrics/restore")
    def restore(tid: str, request: Request):
        """手動の歌詞を、書き換える前の版に戻す(タイミングの記録・貼り付け・削除の取り消し)。"""
        track(tid)
        uid = member(request)
        if not (store.restore_personal(uid, tid) if uid is not None else store.restore_prev(tid)):
            raise HTTPException(409, "戻せる版がありません")
        return out(tid, uid)

    return r
