import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from otokura.lyrics.api import make_router
from otokura.lyrics.lrclib import LrcLib
from otokura.lyrics.store import LyricsStore, TrackRef

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg が必要")


def fake_lrclib(calls):
    def handler(req: httpx.Request):
        calls.append(req.url.path)
        q = dict(req.url.params)
        if req.url.path == "/api/get":
            if q.get("track_name") == "手紙":
                return httpx.Response(200, json={"duration": 207, "syncedLyrics": "[00:01.00]取ってきた歌詞", "plainLyrics": "取ってきた歌詞", "instrumental": False})
            return httpx.Response(404, json={})
        if req.url.path == "/api/search":
            if q.get("track_name") == "朝":
                return httpx.Response(200, json=[
                    {"duration": 300, "syncedLyrics": "[00:01.00]長さ違い"},
                    {"duration": 181, "plainLyrics": "同期なし"},
                    {"duration": 180.5, "syncedLyrics": "[00:01.00]正解"}])
            return httpx.Response(200, json=[])
        return httpx.Response(404)
    return LrcLib(client=httpx.Client(transport=httpx.MockTransport(handler)), base="https://lrclib.test", min_interval_s=0)


@pytest.fixture
def env(tmp_path):
    def mk(name, secs):
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", str(secs), str(p)], check=True)
        return p
    tracks = {
        "1": TrackRef("1", str(mk("a.flac", 3)), "手紙", "潮見 すず", "海沿いの手紙", 207000),
        "2": TrackRef("2", str(mk("b.flac", 3)), "朝", "x", None, 180000),
        "3": TrackRef("3", str(mk("c.flac", 3)), "無い曲", "x", None, 100000),
    }
    calls = []
    store = LyricsStore(tmp_path / "otokura.db", lrclib=fake_lrclib(calls))
    app = FastAPI()
    app.include_router(make_router(store, tracks.get), prefix="/api")
    return TestClient(app), store, tracks, tmp_path, calls


def test_local_then_sidecar_update(env):
    c, store, tracks, tmp, _ = env
    r = c.get("/api/tracks/1/lyrics").json()
    assert r["available"] is False and r["canFetch"] is True
    lrc = tmp / "a.lrc"
    lrc.write_text("[00:01.00]手元の歌詞", encoding="utf-8")
    r = c.get("/api/tracks/1/lyrics").json()
    assert r["lyrics"]["source"] == "sidecar:lrc" and r["lyrics"]["synced"] is True
    # 書き換えると読み直す(mtime を確実に進める)
    lrc.write_text("[00:01.00]書き換えた", encoding="utf-8")
    os.utime(lrc, (lrc.stat().st_mtime + 5, lrc.stat().st_mtime + 5))
    r = c.get("/api/tracks/1/lyrics").json()
    assert r["lyrics"]["layers"][0]["lines"][0]["text"] == "書き換えた"
    assert store.refresh(tracks["1"]) is False     # 変化なしなら何もしない


def test_fetch_exact_and_priority(env):
    c, store, tracks, tmp, _ = env
    r = c.post("/api/tracks/1/lyrics/fetch").json()
    assert r["lyrics"]["source"] == "lrclib"
    assert r["lyrics"]["layers"][0]["lines"][0]["text"] == "取ってきた歌詞"
    (tmp / "a.lrc").write_text("[00:01.00]手元が勝つ", encoding="utf-8")
    r = c.get("/api/tracks/1/lyrics").json()
    assert r["lyrics"]["source"] == "sidecar:lrc"
    r = c.put("/api/tracks/1/lyrics", json={"text": "[00:02.00]手動が最優先"}).json()
    assert r["lyrics"]["source"] == "manual"
    r = c.delete("/api/tracks/1/lyrics/manual").json()
    assert r["lyrics"]["source"] == "sidecar:lrc"


def test_fetch_search_filters_duration(env):
    c, *_ = env
    r = c.post("/api/tracks/2/lyrics/fetch").json()
    assert r["lyrics"]["layers"][0]["lines"][0]["text"] == "正解"


def test_miss_is_remembered(env):
    c, store, tracks, _, calls = env
    r = c.post("/api/tracks/3/lyrics/fetch").json()
    assert r["available"] is False and r["lrclibMiss"] is True
    n = len(calls)
    assert store.fetch_lrclib(tracks["3"]) is None and len(calls) == n   # 自動取得は 30 日待つ
    assert "3" not in store.missing()


def test_offset_and_upload_cp932(env):
    c, *_ = env
    r = c.put("/api/tracks/3/lyrics/offset", json={"offsetMs": 99999}).json()
    assert r["offsetMs"] == 10000
    r = c.post("/api/tracks/3/lyrics/upload",
               files={"file": ("x.lrc", "[00:01.00]アップロード".encode("cp932"))}).json()
    assert r["lyrics"]["layers"][0]["lines"][0]["text"] == "アップロード"


def test_unknown_track(env):
    c, *_ = env
    assert c.get("/api/tracks/zzz/lyrics").status_code == 404


def test_rate_limited_is_reported(tmp_path):
    from otokura.lyrics.lrclib import LrcLib, Query, RateLimited
    lib = LrcLib(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "30"}))), base="https://x.test", min_interval_s=0)
    with pytest.raises(RateLimited) as e:
        lib.fetch(Query("a", "b"))
    assert e.value.retry_after == 30


def test_min_interval(tmp_path):
    import time
    from otokura.lyrics.lrclib import LrcLib, Query
    lib = LrcLib(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                 base="https://x.test", min_interval_s=0.2)
    t = time.monotonic(); lib.fetch(Query("a", "b")); lib.fetch(Query("a", "b"))
    assert time.monotonic() - t >= 0.6      # 4 回の問い合わせ、間隔 0.2 秒
