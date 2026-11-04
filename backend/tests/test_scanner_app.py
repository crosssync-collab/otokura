import os
import time

import pytest
from fastapi.testclient import TestClient

from otokura.app import create_app
from otokura.config import parse_sources
from otokura.metadata import guess_from_path
from pathlib import Path


def test_parse_sources():
    s = parse_sources("Lossless=/music/Lossless, 1.Lossy=/music/1.Lossy,/music/Extra")
    assert [(x.id, x.name, str(x.path)) for x in s] == [
        ("lossless", "Lossless", "/music/Lossless"), ("1lossy", "1.Lossy", "/music/1.Lossy"), ("extra", "Extra", "/music/Extra")]


def test_guess_from_path():
    g = guess_from_path(Path("Artist/Album (1998)/CD2/03 - Someone - Title Here.flac"))
    assert g == {"track_no": 3, "artist": "Someone", "title": "Title Here", "year": 1998, "disc_no": 2,
                 "album": "Album", "albumartist": "Artist"}
    assert guess_from_path(Path("01.夜明け.flac"))["title"] == "夜明け"


@pytest.fixture
def client(cfg):
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    return TestClient(app), app


def lib(c):
    d = c.get("/api/library").json()
    tc, ac = d["trackColumns"], d["albumColumns"]
    tracks = [dict(zip(tc, r)) for r in d["tracks"]]
    albums = [dict(zip(ac, r)) for r in d["albums"]]
    return d, tracks, albums


def test_scan_reads_everything(client):
    c, app = client
    st = app.state.scanner.state
    assert st.added == 5 and not st.errors, st.errors
    d, tracks, albums = lib(c)
    by = {t["title"]: t for t in tracks}
    assert set(by) == {"夜行バス", "窓の外の街", "Glass Orchard", "朝焼け前", "tta song"}
    # 文字化け修復
    assert by["朝焼け前"]["artist"] == "潮見 すず" and by["朝焼け前"]["tagFixed"] == 1
    # タグなし → ファイル名・フォルダから
    g = by["Glass Orchard"]
    assert g["tagGuessed"] == 1 and g["no"] == 3
    alb = {a["title"]: a for a in albums}
    assert alb["Tessellate"]["year"] == 2024 and alb["Tessellate"]["artist"] == "Marrow & Fern"
    # カバー(埋め込み / フォルダ)と色相
    assert alb["夜行バス"]["hasArt"] == 1 and alb["Tessellate"]["hasArt"] == 1
    assert abs(alb["夜行バス"]["hue"] - 350) < 15 and abs(alb["Tessellate"]["hue"] - 220) < 15
    assert alb["夜行バス"]["year"] == 2023 and alb["夜行バス"]["genre"] == "アンビエント"
    # 同じアルバムの 2 曲は 1 枚にまとまる
    assert by["夜行バス"]["albumId"] == by["窓の外の街"]["albumId"]
    assert by["tta song"]["codec"] == "TTA"


def test_stream_range_and_art(client):
    c, _ = client
    _, tracks, albums = lib(c)
    t = next(x for x in tracks if x["codec"] == "FLAC")
    r = c.get(f"/api/tracks/{t['id']}/stream", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"] == "audio/flac"
    tta = next(x for x in tracks if x["codec"] == "TTA")
    r = c.get(f"/api/tracks/{tta['id']}/stream")
    assert r.status_code == 415 and "次の段階" in r.json()["detail"]
    a = next(x for x in albums if x["hasArt"])
    r = c.get(f"/api/art/{a['id']}?size=1000")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"


def test_stats_survive_rescan(client):
    c, app = client
    _, tracks, _ = lib(c)
    tid = next(x["id"] for x in tracks if x["title"] == "朝焼け前")
    c.post(f"/api/tracks/{tid}/played"); c.post(f"/api/tracks/{tid}/played"); c.post(f"/api/tracks/{tid}/skipped")
    r = c.put(f"/api/tracks/{tid}/stats", json={"fav": True, "rating": 9}).json()
    assert r == {**r, "plays": 2, "skips": 1, "fav": True, "rating": 5}
    app.state.scanner.run(full=True)
    _, tracks, _ = lib(c)
    t = next(x for x in tracks if x["title"] == "朝焼け前")
    assert (t["plays"], t["fav"], t["rating"]) == (2, 1, 5)


def test_incremental_and_removed(client, library):
    c, app = client
    st = app.state.scanner.run()
    assert (st.added, st.updated, st.removed) == (0, 0, 0)
    p = library["lossless"] / "Marrow & Fern" / "Tessellate [2024]" / "03 - Glass Orchard.flac"
    os.utime(p, (time.time() + 10, time.time() + 10))
    st = app.state.scanner.run()
    assert st.updated == 1
    p.unlink()
    st = app.state.scanner.run()
    assert st.removed == 1
    _, tracks, albums = lib(c)
    assert "Tessellate" not in {a["title"] for a in albums}     # 空のアルバムは消える


def test_missing_source_does_not_wipe(client, library):
    c, app = client
    import shutil
    shutil.move(str(library["lossy"]), str(library["root"] / "moved"))
    st = app.state.scanner.run()
    assert st.removed == 0 and st.missing_sources == ["1.Lossy"]
    assert len(lib(c)[1]) == 5


def test_lyrics_via_scan(client):
    c, _ = client
    _, tracks, _ = lib(c)
    tid = next(x["id"] for x in tracks if x["title"] == "夜行バス")
    r = c.get(f"/api/tracks/{tid}/lyrics").json()
    assert r["available"] and r["lyrics"]["source"] == "sidecar:lrc"


def test_playlists_and_settings(client):
    c, app = client
    _, tracks, _ = lib(c)
    ids = [t["id"] for t in tracks[:3]]
    p = c.post("/api/playlists", json={"name": "作業用", "trackIds": ids}).json()
    assert p["trackIds"] == ids
    p = c.put(f"/api/playlists/{p['id']}", json={"addTrackIds": [tracks[3]["id"]], "name": "作業用2"}).json()
    assert p["name"] == "作業用2" and len(p["trackIds"]) == 4
    app.state.scanner.run(full=True)
    assert c.get("/api/playlists").json()[0]["trackIds"] == ids + [tracks[3]["id"]]
    assert c.post("/api/playlists", json={"name": " "}).status_code == 422
    assert c.put("/api/settings", json={"theme": "dark", "mixSize": 40}).json() == {"theme": "dark", "mixSize": 40}
    c.delete(f"/api/playlists/{p['id']}")
    assert c.get("/api/playlists").json() == []


def test_server_and_index(client):
    c, _ = client
    s = c.get("/api/server").json()
    assert [x["tracks"] for x in s["sources"]] == [4, 1]
    assert {x["codec"]: x["playable"] for x in s["codecs"]}["TTA"] is False
    html = c.get("/").text
    assert "window.OTOKURA_API=''" in html and "<title>音蔵</title>" in html


def test_pwa_files(client):
    c, _ = client
    assert 'rel="manifest"' in c.get("/").text
    m = c.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["display"] == "fullscreen" and {i["sizes"] for i in m.json()["icons"]} >= {"192x192", "512x512"}
    sw = c.get("/sw.js")
    assert "javascript" in sw.headers["content-type"] and sw.headers["cache-control"] == "no-cache"
    assert c.get("/icon-192.png").headers["content-type"] == "image/png"
    assert c.get("/index.html").status_code == 404 and c.get("/nothing.png").status_code == 404
    # ミックスのジャケット
    assert c.get("/mix/1980.jpg").headers["content-type"] == "image/jpeg"
    assert c.get("/mix/credits.json").status_code == 404 and c.get("/mix/..%2Fapp.py").status_code == 404
