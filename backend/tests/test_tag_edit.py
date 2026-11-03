import pytest
from fastapi.testclient import TestClient

from otokura import tagedit
from otokura.app import create_app

from conftest import make_audio, png_bytes


@pytest.fixture
def client(cfg):
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    return TestClient(app)


def tracks(c):
    d = c.get("/api/library").json()
    return [dict(zip(d["trackColumns"], r)) for r in d["tracks"]], [dict(zip(d["albumColumns"], r)) for r in d["albums"]]


def find(c, title):
    return next(t for t in tracks(c)[0] if t["title"] == title)


@pytest.mark.parametrize("ext,args", [("flac", ()), ("mp3", ("-c:a", "libmp3lame")), ("m4a", ("-c:a", "aac")),
                                      ("ogg", ("-c:a", "libvorbis")), ("opus", ("-c:a", "libopus"))])
def test_write_fields_tags_cover_each_format(tmp_path, ext, args):
    p = make_audio(tmp_path / f"a.{ext}", codec_args=args)
    res = tagedit.write(p, {"title": "曲名", "artist": "歌う人", "tracknumber": "3/12", "date": "2021", "comment": "メモ"})
    assert set(res["before"].values()) == {None}
    r = tagedit.read(p)
    assert r["fields"]["title"] == "曲名" and r["fields"]["artist"] == "歌う人"
    assert r["fields"]["tracknumber"] in ("3/12", "3") and r["fields"]["date"] == "2021" and r["fields"]["comment"] == "メモ"
    # 空にすると消える
    tagedit.write(p, {"comment": ""})
    assert tagedit.read(p)["fields"]["comment"] == ""
    # カバー
    tagedit.write(p, cover=png_bytes())
    assert tagedit.read(p)["hasCover"] and tagedit.get_cover(p)[:4] == b"\x89PNG"
    tagedit.write(p, remove_cover=True)
    assert not tagedit.read(p)["hasCover"]


def test_raw_tags_vorbis_and_id3(tmp_path):
    f = make_audio(tmp_path / "a.flac")
    tagedit.write(f, tags={"MOOD": ["静か", "夜"]})
    e = {x["key"]: x for x in tagedit.read(f)["tags"]}
    assert e["MOOD"]["values"] == ["静か", "夜"] and e["MOOD"]["editable"]
    m = make_audio(tmp_path / "a.mp3", codec_args=("-c:a", "libmp3lame"))
    tagedit.write(m, tags={"TXXX:MOOD": ["静か"], "TBPM": ["120"]})
    e = {x["key"]: x for x in tagedit.read(m)["tags"]}
    assert e["TXXX:MOOD"]["values"] == ["静か"] and e["TBPM"]["values"] == ["120"]
    tagedit.write(m, cover=png_bytes())
    apic = next(x for x in tagedit.read(m)["tags"] if x["key"].startswith("APIC"))
    assert not apic["editable"]
    with pytest.raises(tagedit.TagError):
        tagedit.write(m, tags={apic["key"]: ["x"]})


def test_mojibake_shown_fixed_and_rewritten(client, cfg):
    c = client
    t = find(c, "朝焼け前")
    m = c.get(f"/api/tracks/{t['id']}/meta").json()
    assert m["kind"] == "id3" and m["fields"]["artist"] == "潮見 すず" and m["file"]["codec"] == "MP3"
    assert m["writable"] and m["history"] == []
    # 曲名だけ直す → ほかは化けたまま(書き換えたキーだけ書く)
    assert c.put(f"/api/tracks/{t['id']}/meta", json={"fields": {"title": "朝焼けの前"}}).status_code == 200
    raw = {x["key"]: x for x in tagedit._entries(*_open_kind(cfg.sources[1].path / "single" / "track.mp3"))}
    assert raw["TIT2"]["values"] == ["朝焼けの前"]
    assert raw["TPE1"]["values"] != ["潮見 すず"]      # 化けたまま(表示では直す)


def _open_kind(p):
    f = tagedit._open(p)
    return f, tagedit._kind(f)


def test_edit_and_revert_updates_library(client):
    c = client
    t = find(c, "夜行バス")
    assert c.put(f"/api/tracks/{t['id']}/meta", json={"fields": {"title": "夜行バス(改)", "genre": ""}}).status_code == 200
    t2 = find(c, "夜行バス(改)")
    h = c.get(f"/api/tracks/{t2['id']}/meta").json()["history"]
    assert h[0]["keys"] == ["TITLE", "GENRE"] and not h[0]["reverted"]
    assert c.post(f"/api/tagedits/{h[0]['id']}/revert").status_code == 200
    m = c.get(f"/api/tracks/{find(c, '夜行バス')['id']}/meta").json()
    assert m["fields"]["genre"] == "アンビエント" and m["history"][0]["reverted"]
    assert c.post(f"/api/tagedits/{h[0]['id']}/revert").status_code == 409


def test_album_fields_and_cover(client):
    c = client
    tr, al = tracks(c)
    aid = next(a["id"] for a in al if a["title"] == "夜行バス")
    m = c.get(f"/api/albums/{aid}/meta").json()
    assert m["tracks"] == 2 and m["fields"]["album"] == "夜行バス" and m["fields"]["date"] == "2023"
    assert c.put(f"/api/albums/{aid}/meta", json={"fields": {"title": "x"}}).status_code == 400
    assert c.put(f"/api/albums/{aid}/meta", json={"fields": {"album": "夜行バス 完全版", "date": "2024"}}).json()["tracks"] == 2
    tr, al = tracks(c)
    a = next(a for a in al if a["title"] == "夜行バス 完全版")
    assert a["year"] == 2024 and not any(x["title"] == "夜行バス" for x in al)
    old_art = a["art"]
    from PIL import Image
    import io
    b = io.BytesIO(); Image.new("RGB", (64, 64), (10, 200, 40)).save(b, "JPEG")
    assert c.put(f"/api/albums/{a['id']}/cover", content=b.getvalue()).json()["tracks"] == 2
    a2 = next(x for x in tracks(c)[1] if x["title"] == "夜行バス 完全版")
    assert a2["art"] and a2["art"] != old_art
    assert c.get(f"/api/tracks/{find(c, '窓の外の街')['id']}/cover").content[:3] == b"\xff\xd8\xff"
    # まとめて元に戻す
    h = c.get(f"/api/albums/{a2['id']}/meta").json()["history"]
    assert h[0]["keys"] == ["カバー"] and h[0]["tracks"] == 2
    c.post(f"/api/tagedits/{h[0]['id']}/revert")
    assert c.get(f"/api/tracks/{find(c, '夜行バス')['id']}/cover").content[:4] == b"\x89PNG"
    assert c.get(f"/api/tracks/{find(c, '窓の外の街')['id']}/cover").status_code == 404


def test_readonly_file_is_refused(client, cfg):
    c = client
    t = find(c, "窓の外の街")
    p = cfg.sources[0].path / t["rel"]
    p.chmod(0o444)
    try:
        if p.stat().st_mode & 0o200 == 0 and not __import__("os").access(p, __import__("os").W_OK):
            assert c.put(f"/api/tracks/{t['id']}/meta", json={"fields": {"title": "x"}}).status_code == 409
    finally:
        p.chmod(0o644)
