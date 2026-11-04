import io
import time

import httpx
import pytest
from PIL import Image

from otokura.db import Database
from otokura.webinfo import Transient, WebInfo, norm


def jpeg():
    b = io.BytesIO()
    Image.new("RGB", (500, 500), (20, 120, 200)).save(b, "JPEG")
    return b.getvalue()


class Net:
    """MusicBrainz などの応答を差し替える。"""

    def __init__(self):
        self.calls, self.summary, self.fail = [], "夜行バスで知られる日本の歌手。", False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path, q = req.url.host, req.url.path, dict(req.url.params)
        self.calls.append(f"{host}{path}")
        if self.fail:
            return httpx.Response(503, headers={"retry-after": "0"})
        if host == "musicbrainz.org":
            if path == "/ws/2/artist":
                return httpx.Response(200, json={"artists": [
                    {"id": "right", "name": "深谷 あおい"}, {"id": "same", "name": "深谷あおい"}, {"id": "other", "name": "別の人"}]})
            if path == "/ws/2/release-group" and q.get("artist") == "right":
                return httpx.Response(200, json={"release-groups": [{"title": "夜行バス"}]})
            if path == "/ws/2/release-group" and q.get("artist"):
                return httpx.Response(200, json={"release-groups": [{"title": "別のアルバム"}]})
            if path == "/ws/2/release-group":
                return httpx.Response(200, json={"release-groups": [
                    {"id": "rg0", "title": "夜行バス", "artist-credit": [{"name": "別の人"}]},
                    {"id": "rg1", "title": "夜行バス", "primary-type": "Album", "first-release-date": "2023-04-05",
                     "artist-credit": [{"name": "深谷 あおい"}]}]})
            if path == "/ws/2/release":
                return httpx.Response(200, json={"releases": [{"label-info": [{"label": {"name": "蔵レコード"}}]}]})
            if path.startswith("/ws/2/artist/right") or path.startswith("/ws/2/release-group/rg1"):
                return httpx.Response(200, json={"relations": [{"url": {"resource": "https://www.wikidata.org/wiki/Q42"}}]})
        if host == "www.wikidata.org":
            return httpx.Response(200, json={"entities": {"Q42": {
                "claims": {"P18": [{"mainsnak": {"datavalue": {"value": "Aoi Fukaya.jpg"}}}]},
                "sitelinks": {"jawiki": {"title": "深谷 あおい"}}}}})
        if host == "ja.wikipedia.org":
            return httpx.Response(200, json={"type": "standard", "extract": self.summary,
                                             "content_urls": {"desktop": {"page": "https://ja.wikipedia.org/wiki/x"}}})
        if host == "api.deezer.com" and path == "/search/artist":
            return httpx.Response(200, json={"data": [{"id": 1, "name": "深谷 あおい", "picture_xl": "https://cdn.example/a.jpg"}]})
        if host == "api.deezer.com":
            return httpx.Response(200, json={"data": [{"title": "夜行バス (Deluxe)"}]})
        if host in ("cdn.example", "commons.wikimedia.org"):
            return httpx.Response(200, content=jpeg())
        if host == "itunes.apple.com":
            return httpx.Response(200, json={"results": [
                {"collectionName": "孤島", "artistName": "潮見 すず", "releaseDate": "2019-02-03T08:00:00Z"}]})
        return httpx.Response(404)


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr("otokura.webinfo.INTERVAL_S", {})
    monkeypatch.setattr("otokura.webinfo.DEFAULT_INTERVAL_S", 0)
    db = Database(tmp_path / "d.db")
    for i, (t, a) in enumerate([("夜行バス", "深谷 あおい"), ("孤島", "潮見 すず"), ("名無し", "Various Artists")]):
        db.conn.execute("INSERT INTO albums(key,title,artist,source,folder,added_at) VALUES(?,?,?,'m','f',0)", (f"k{i}", t, a))
    db.conn.commit()
    net = Net()
    w = WebInfo(db, tmp_path / "art", httpx.Client(transport=httpx.MockTransport(net)))
    return w, db, net


def test_norm():
    assert norm("深谷 あおい") == norm("深谷あおい") == norm("深谷　あおい")
    assert norm("ＡＢＣ-1") == "abc1"


def test_artist_picks_album_overlap_and_saves(web):
    w, db, net = web
    w.update_artist("深谷 あおい")
    r = db.one("SELECT * FROM artist_info WHERE name=?", ("深谷 あおい",))
    assert r["mbid"] == "right"                            # 同じ名前の 2 人から、手元のアルバムと重なる人を選ぶ
    assert r["status"] == "ok" and r["summary"].startswith("夜行バス") and r["image_source"] == "Deezer" and r["image"]
    assert (w.art_dir / f"{r['image']}_300.jpg").is_file()


def test_various_artists_skips_network(web):
    w, db, net = web
    w.update_artist("Various Artists")
    assert net.calls == [] and db.one("SELECT status FROM artist_info")["status"] == "none"


def test_album_exact_match_and_itunes_fallback(web):
    w, db, net = web
    w.update_album("k0")
    r = db.one("SELECT * FROM album_info WHERE key='k0'")
    assert (r["mbid"], r["release_date"], r["kind"], r["label"], r["source"]) == ("rg1", "2023-04-05", "Album", "蔵レコード", "MusicBrainz")
    w.update_album("k1")      # MusicBrainz に無いので iTunes の発売日
    r = db.one("SELECT * FROM album_info WHERE key='k1'")
    assert (r["release_date"], r["source"], r["status"]) == ("2019-02-03", "iTunes", "ok")


def test_update_check_detects_change_and_keeps_data_on_failure(web):
    w, db, net = web
    assert w.update_artist("深谷 あおい") is False         # 初回は「更新」ではない
    first = db.one("SELECT * FROM artist_info")
    assert w.update_artist("深谷 あおい") is False         # 同じ内容
    net.summary = "新しい紹介文。"
    assert w.update_artist("深谷 あおい") is True
    r = db.one("SELECT * FROM artist_info")
    assert r["summary"] == "新しい紹介文。" and r["updated_at"] > first["first_at"]
    assert w.public()["recent"][0]["name"] == "深谷 あおい"
    net.fail = True                                        # 通信の失敗では消さない
    with pytest.raises(Transient):
        w.update_artist("深谷 あおい")
    assert db.one("SELECT summary FROM artist_info")["summary"] == "新しい紹介文。"


def test_queue_missing_stale_and_daily(web):
    w, db, net = web
    assert w.enqueue_missing() == 6      # アーティスト 3 + アルバム 3
    w.wait(20)
    st = w.public()
    assert st["running"] is False and st["artists"]["ok"] == 1 and st["albums"]["ok"] == 2 and st["errors"] == 0
    assert w.enqueue_missing() == 0 and w.enqueue_stale(30) == 0
    w.wait(5)
    db.conn.execute("UPDATE artist_info SET fetched_at=?", (time.time() - 40 * 86400,))
    db.conn.commit()
    assert w.daily() == 3                # 古くなった分だけ
    assert w.daily() == 0                # 1 日 1 回
    w.wait(20)
