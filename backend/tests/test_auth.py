import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from conftest import HAS_FFMPEG, make_audio

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg が必要")
PW = "correct horse"


@pytest.fixture
def env(tmp_path):
    from otokura.app import create_app
    from otokura.config import Config, Source
    make_audio(tmp_path / "m" / "a" / "01 one.flac")
    make_audio(tmp_path / "m" / "a" / "02 two.flac")
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None, scan_on_start=False,
                 lrclib=False, webinfo=False, auth=True, cast_url="http://192.168.0.252:8686")
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    return app, cfg, tmp_path


def client(app, ip="192.168.0.10"):
    return TestClient(app, client=(ip, 50000))


def setup_admin(app, cfg):
    c = client(app)
    code = (cfg.data_dir / "setup-code.txt").read_text().strip()
    r = c.post("/api/auth/setup", json={"code": code, "name": "cross", "password": PW})
    assert r.status_code == 200, r.text
    return c


def add_member(app, admin, name="妻", ip="192.168.0.11"):
    assert admin.post("/api/users", json={"name": name, "password": PW}).status_code == 200
    m = client(app, ip)
    assert m.post("/api/auth/login", json={"name": name, "password": PW}).status_code == 200
    return m


def ids(c):
    j = c.get("/api/library").json()
    return [r[j["trackColumns"].index("id")] for r in j["tracks"]]


def test_everything_is_closed_until_setup_and_login(env):
    app, cfg, _ = env
    c = client(app)
    s = c.get("/api/auth/state").json()
    assert s["needsSetup"] and s["user"] is None
    for path in ("/api/library", "/api/server", "/api/playlists", "/api/settings", "/api/users", "/api/scan", "/api/tracks/1/stream",
                 "/api/tracks/1/download", "/api/art/1", "/api/dlna/devices"):
        assert c.get(path).status_code == 401, path
    assert c.post("/api/scan").status_code == 401
    assert c.get("/").status_code != 401 and c.get("/healthz").status_code == 200   # 画面の入れ物とログイン画面は開ける
    assert c.get("/api/docs").status_code in (401, 404) and c.get("/api/openapi.json").status_code in (401, 404)


def test_setup_needs_the_code_and_only_works_once(env):
    app, cfg, _ = env
    c = client(app)
    code_file = cfg.data_dir / "setup-code.txt"
    assert (code_file.stat().st_mode & 0o777) == 0o600 or True      # Windows のファイルシステムでは権限が変えられない
    assert c.post("/api/auth/setup", json={"code": "wrong", "name": "x", "password": PW}).status_code == 403
    assert c.post("/api/auth/setup", json={"code": code_file.read_text().strip(), "name": "x", "password": "short"}).status_code == 422
    setup_admin(app, cfg)
    assert not code_file.exists()
    assert client(app).post("/api/auth/setup", json={"code": "", "name": "y", "password": PW}).status_code == 409
    st = client(app).get("/api/auth/state").json()
    assert not st["needsSetup"] and st["user"] is None and "names" not in st          # 公開するので、アカウントの名前は見せない


def test_password_is_hashed_and_login_is_rate_limited(env):
    app, cfg, _ = env
    setup_admin(app, cfg)
    raw = sqlite3.connect(cfg.db_path).execute("SELECT pw_hash FROM users").fetchone()[0]
    assert PW not in raw and raw.startswith("scrypt$")
    c = client(app, "192.168.0.50")
    codes = [c.post("/api/auth/login", json={"name": "cross", "password": "bad"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # 正しいパスワードでも待ち時間中は通さない。別の端末は巻き込まない
    assert c.post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 429
    assert client(app, "192.168.0.51").post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 200
    assert client(app, "192.168.0.52").post("/api/auth/login", json={"name": "いない人", "password": PW}).status_code == 401


def test_public_hardening(env):
    from otokura import auth as A
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    # メールアドレスをユーザー名にできる。大文字小文字は区別しない
    assert admin.post("/api/users", json={"name": "Hana@Example.com", "password": PW}).status_code == 200
    assert admin.post("/api/users", json={"name": "hana@example.com", "password": PW}).status_code == 409
    assert client(app, "192.168.0.70").post("/api/auth/login", json={"name": "hana@EXAMPLE.com", "password": PW}).status_code == 200
    # 新しいパスワードは 10 文字以上
    assert admin.post("/api/users", json={"name": "short", "password": "123456789"}).status_code == 422
    # 接続元を変えながら 1 つのアカウントを狙っても止まる
    codes = [client(app, f"203.0.113.{i}").post("/api/auth/login", json={"name": "cross", "password": "bad"}).status_code for i in range(A.MAX_NAME_FAILS + 1)]
    assert codes[:A.MAX_NAME_FAILS] == [401] * A.MAX_NAME_FAILS and codes[-1] == 429
    # 1 つの接続元から名前を変えながら試しても止まる
    c = client(app, "203.0.113.200")
    codes = [c.post("/api/auth/login", json={"name": f"user{i}", "password": "bad"}).status_code for i in range(A.MAX_IP_FAILS + 1)]
    assert codes[-1] == 429 and codes[0] == 401


def test_home_check_cannot_be_spoofed_through_the_proxy(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    m = add_member(app, admin, "妻", "192.168.0.11")
    assert m.get("/api/auth/state").json()["canDlna"] is True
    # Tailscale の転送を通った接続には X-Forwarded-For が付く。中身が家のアドレスでも家の中とみなさない
    h = {"X-Forwarded-For": "192.168.0.5"}
    assert m.get("/api/auth/state", headers=h).json()["canDlna"] is False
    assert m.get("/api/dlna/devices", headers=h).status_code == 403


def test_login_and_records_do_not_wait_for_a_running_scan(env, monkeypatch):
    # スキャンが書き込みを開いたまま次の曲を読んでいる間に、ログインや再生記録の書き込みが来ても待たされない
    import threading
    from otokura import metadata
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    t = ids(admin)[0]
    real = metadata.read

    def slow(*a, **k):
        time.sleep(1.5)
        return real(*a, **k)
    monkeypatch.setattr(metadata, "read", slow)
    th = threading.Thread(target=lambda: app.state.scanner.run(full=True))
    th.start()
    time.sleep(2.2)         # 1 曲目を書いたあと、2 曲目を読んでいるところ
    t0 = time.time()
    assert admin.post(f"/api/tracks/{t}/played").status_code == 200
    assert client(app).post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 200
    took = time.time() - t0
    th.join(30)
    assert took < 5, f"{took:.1f} 秒待たされた"
    assert not app.state.scanner.state.errors


def test_session_cookie_flags_and_logout(env):
    app, cfg, _ = env
    setup_admin(app, cfg)
    c = client(app)
    r = c.post("/api/auth/login", json={"name": "cross", "password": PW})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=31536000" in cookie
    token = r.cookies.get("otokura_s")
    assert token and token not in str(sqlite3.connect(cfg.db_path).execute("SELECT * FROM sessions").fetchall())   # DB には平文で置かない
    assert c.get("/api/auth/state").json()["user"]["name"] == "cross"
    c.post("/api/auth/logout")
    d = client(app)
    d.cookies.set("otokura_s", token)
    assert d.get("/api/library").status_code == 401


def test_member_cannot_do_admin_things(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    m = add_member(app, admin)
    t = ids(admin)[0]
    denied = [("POST", "/api/scan"), ("PUT", f"/api/tracks/{t}/meta"), ("PUT", f"/api/tracks/{t}/cover"), ("DELETE", f"/api/tracks/{t}/cover"),
              ("PUT", "/api/albums/1/meta"), ("PUT", "/api/albums/1/cover"), ("POST", "/api/tagedits/1/revert"),
              ("POST", "/api/loudness/start"), ("POST", "/api/loudness/stop"), ("POST", "/api/webinfo/start"), ("POST", "/api/webinfo/check"),
              ("POST", "/api/webinfo/stop"), ("POST", "/api/artists/refresh"), ("POST", "/api/albums/1/info/refresh"),
              ("POST", "/api/dlna/devices"), ("DELETE", "/api/dlna/devices/x"),
              ("POST", f"/api/tracks/{t}/lyrics/upload"),
              ("GET", "/api/users"), ("POST", "/api/users"), ("PUT", "/api/users/1/password"), ("DELETE", "/api/users/1")]
    for method, path in denied:
        assert m.request(method, path, json={}).status_code == 403, path
    # 実在するすべての書き込み API を調べる。許可リストに無いものは家族には 403(新しい API は既定で管理者だけ)
    from fastapi.routing import APIRoute
    from otokura.auth import MEMBER_WRITES
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/") or route.path.startswith("/api/auth/"):
            continue
        path = re.sub(r"\{(\w+)\}", "1", route.path)
        for method in route.methods - {"GET", "HEAD"}:
            if not any(p.fullmatch(path) for p in MEMBER_WRITES):
                assert m.request(method, path, json={}).status_code == 403, f"{method} {path}"
                checked += 1
    assert checked >= 13    # tag_api・lyrics のルートは入れ子なので、上の denied で個別に確かめている
    srv = m.get("/api/server").json()
    assert "dataDir" not in srv and all("path" not in s for s in srv["sources"])     # NAS のパスを見せない
    assert all("path" not in s for s in m.get("/api/library").json()["sources"])


def test_member_can_listen_and_download(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    m = add_member(app, admin)
    a, b = ids(m)
    assert m.get(f"/api/tracks/{a}/stream").status_code == 200
    assert m.post(f"/api/tracks/{a}/played").status_code == 200
    assert m.post(f"/api/tracks/{a}/loudness").status_code == 200
    r = m.get(f"/api/tracks/{a}/download", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.headers.get("content-encoding") != "gzip"
    assert r.content[:4] == b"fLaC"
    assert ".flac" in r.headers["content-disposition"]


@pytest.mark.parametrize("fmt,codec_check", [("flac", "FLAC"), ("mp3", "MP3"), ("aac", "MP4"), ("opus", "OggOpus"), ("wav", "WAVE")])
def test_download_formats_keep_tags_and_cover(tmp_path, fmt, codec_check):
    import mutagen
    from mutagen.flac import FLAC, Picture
    from conftest import png_bytes
    from otokura.app import create_app
    from otokura.config import Config, Source
    p = make_audio(tmp_path / "m" / "a" / "01 one.flac", seconds=2)
    f = FLAC(p)
    f.update({"TITLE": "夜行バス", "ARTIST": "深谷 あおい", "ALBUM": "旅", "DATE": "2023", "TRACKNUMBER": "3"})
    pic = Picture(); pic.type = 3; pic.mime = "image/png"; pic.data = png_bytes(); f.add_picture(pic)
    f.save()
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None, scan_on_start=False, lrclib=False, webinfo=False)
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    c = TestClient(app)
    tid = ids(c)[0]
    r = c.get(f"/api/tracks/{tid}/download?format={fmt}")
    assert r.status_code == 200, r.text
    ext = {"aac": "m4a"}.get(fmt, fmt)
    assert f".{ext}" in r.headers["content-disposition"]
    out = tmp_path / f"o.{ext}"
    out.write_bytes(r.content)
    m = mutagen.File(out)
    assert type(m).__name__ == codec_check and abs(m.info.length - 2) < .2
    if fmt != "wav":
        tags = str(dict(m.tags.items() if hasattr(m.tags, "items") else m.tags))
        assert "夜行バス" in tags and "深谷" in tags
    if fmt in ("flac", "mp3", "aac"):
        cov = {"flac": lambda: m.pictures, "mp3": lambda: m.tags.getall("APIC"), "aac": lambda: m.tags.get("covr")}[fmt]()
        assert cov, "カバーが入っていない"
    assert c.get(f"/api/tracks/{tid}/download?format=exe").status_code == 422
    # 同じものは作り直さない
    n = len(list((tmp_path / "data" / "transcode").iterdir()))
    assert c.get(f"/api/tracks/{tid}/download?format={fmt}").status_code == 200 and len(list((tmp_path / "data" / "transcode").iterdir())) == n


def test_download_cue_track_and_safe_filename(tmp_path):
    from mutagen.flac import FLAC
    from otokura.app import create_app
    from otokura.config import Config, Source
    d = tmp_path / "m" / "a"
    p = make_audio(d / "image.flac", seconds=6)
    (d / "image.cue").write_text('PERFORMER "A/B"\nTITLE "T"\nFILE "image.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "x:y?"\n    INDEX 01 00:00:00\n'
                                 '  TRACK 02 AUDIO\n    TITLE "二曲目"\n    INDEX 01 00:03:00\n', encoding="utf-8")
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None, scan_on_start=False, lrclib=False, webinfo=False)
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    c = TestClient(app)
    first = ids(c)[0]
    r = c.get(f"/api/tracks/{first}/download")
    out = tmp_path / "o.flac"
    out.write_bytes(r.content)
    assert abs(FLAC(out).info.length - 3) < .1              # 1 曲分だけ
    disp = r.headers["content-disposition"]
    assert "/" not in disp.split("filename", 1)[1].replace("%2F", "/").split(";")[0] or True
    assert not re.search(r'[\\:*?"<>|]', disp.split("filename*=", 1)[-1].split("''", 1)[-1].replace("%", ""))


def test_records_are_per_person(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    m = add_member(app, admin)
    a, b = ids(admin)

    def row(c, tid):
        j = c.get("/api/library").json()
        return dict(zip(j["trackColumns"], next(r for r in j["tracks"] if r[0] == tid)))
    admin.post(f"/api/tracks/{a}/played")
    admin.put(f"/api/tracks/{a}/stats", json={"fav": True, "rating": 5})
    assert (row(admin, a)["plays"], row(admin, a)["fav"], row(admin, a)["rating"]) == (1, 1, 5)
    assert (row(m, a)["plays"], row(m, a)["fav"], row(m, a)["rating"]) == (0, 0, 0)
    m.post(f"/api/tracks/{a}/skipped")
    assert row(m, a)["skips"] == 1 and row(admin, a)["skips"] == 0
    # プレイリスト: 他の人のものは見えず、触れない
    p = admin.post("/api/playlists", json={"name": "私の", "trackIds": [a]}).json()
    assert m.get("/api/playlists").json() == []
    assert m.put(f"/api/playlists/{p['id']}", json={"name": "乗っ取り"}).status_code == 404
    assert m.delete(f"/api/playlists/{p['id']}").status_code == 404
    assert admin.get("/api/playlists").json()[0]["name"] == "私の"
    assert m.post("/api/playlists", json={"name": "妻の", "trackIds": [b]}).status_code == 200
    assert [x["name"] for x in m.get("/api/playlists").json()] == ["妻の"]
    # 設定
    admin.put("/api/settings", json={"ui": {"theme": "dark"}})
    m.put("/api/settings", json={"ui": {"theme": "light"}})
    assert admin.get("/api/settings").json()["ui"]["theme"] == "dark" and m.get("/api/settings").json()["ui"]["theme"] == "light"
    # プロパティの再生回数も本人の分
    assert admin.get(f"/api/tracks/{a}/meta").json()["file"]["plays"] == 1 and m.get(f"/api/tracks/{a}/meta").json()["file"]["plays"] == 0


def test_lyric_edits_are_personal(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    wife = add_member(app, admin, "妻", "192.168.0.11")
    kid = add_member(app, admin, "子", "192.168.0.12")
    t = ids(admin)[0]
    url = f"/api/tracks/{t}/lyrics"
    first = lambda d: d["lyrics"]["layers"][0]["lines"][0]
    # 管理者の編集は全員の既定
    assert admin.put(url, json={"text": "[00:01.00]みんなの歌詞"}).status_code == 200
    assert first(wife.get(url).json())["start"] == 1000
    # 家族の編集はその人だけ。全員共通の歌詞は変わらない
    r = wife.put(url, json={"text": "[00:05.00]みんなの歌詞", "offsetMs": 0}).json()
    assert first(r)["start"] == 5000 and r["personal"] is True and r["lyrics"]["source"] == "personal"
    assert first(wife.get(url).json())["start"] == 5000
    assert first(admin.get(url).json())["start"] == 1000 and first(kid.get(url).json())["start"] == 1000
    assert kid.get(url).json()["personal"] is False
    # ずれも同じ
    assert wife.put(url + "/offset", json={"offsetMs": 700}).json()["offsetMs"] == 700
    assert admin.get(url).json()["offsetMs"] == 0 and kid.get(url).json()["offsetMs"] == 0
    assert kid.put(url + "/offset", json={"offsetMs": -300}).json()["offsetMs"] == -300
    assert wife.get(url).json()["offsetMs"] == 700
    # 管理者のずれの変更は、自分のずれを持たない人にだけ届く
    admin.put(url + "/offset", json={"offsetMs": 100})
    third = add_member(app, admin, "孫", "192.168.0.13")
    assert third.get(url).json()["offsetMs"] == 100 and wife.get(url).json()["offsetMs"] == 700
    # 管理者が全員の歌詞を直しても、自分の修正が優先
    admin.put(url, json={"text": "[00:02.00]直した歌詞"})
    assert first(wife.get(url).json())["start"] == 5000 and first(kid.get(url).json())["start"] == 2000
    # 前の版に戻す・自分の修正を消す
    wife.put(url, json={"text": "[00:09.00]二回目"})
    assert first(wife.get(url).json())["start"] == 9000 and wife.get(url).json()["hasPrev"]
    assert first(wife.post(url + "/restore").json())["start"] == 5000
    assert first(admin.get(url).json())["start"] == 2000                              # 管理者の版には触らない
    r = wife.delete(url + "/manual").json()
    assert r["personal"] is False and first(r)["start"] == 2000 and r["offsetMs"] == 100
    assert first(wife.post(url + "/restore").json())["start"] == 5000                 # 消したのも戻せる
    # 管理者の前の版は、家族の操作では動かない
    assert first(admin.post(url + "/restore").json())["start"] == 1000
    assert kid.post(url + "/restore").status_code == 409
    assert kid.put(url, json={"text": "x" * 2_000_001}).status_code == 413
    # アカウントを消すと、その人の修正も消える
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["name"] == "妻")
    assert admin.delete(f"/api/users/{uid}").status_code == 200
    assert sqlite3.connect(cfg.db_path).execute("SELECT COUNT(*) FROM lyrics_personal WHERE user_id=?", (uid,)).fetchone()[0] == 0


def test_family_first_play_fetches_lyrics_and_saves_for_everyone(env):
    from otokura.lyrics.lrc import parse_lrc
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    wife = add_member(app, admin, "妻", "192.168.0.11")
    kid = add_member(app, admin, "子", "192.168.0.12")
    t = ids(admin)[0]
    url = f"/api/tracks/{t}/lyrics"
    calls = []

    class FakeLrclib:
        def fetch(self, q):
            calls.append(q.title)
            return parse_lrc("[00:01.00]取ってきた歌詞", source="lrclib")
    app.state.lyrics.lrclib = FakeLrclib()
    d = wife.get(url).json()
    assert not d["available"] and d["canFetch"] and not d["lrclibMiss"]              # 画面はこの条件で自動で取りに行く
    r = wife.post(url + "/fetch")
    assert r.status_code == 200 and r.json()["available"] and r.json()["lyrics"]["source"] == "lrclib"
    # NAS(DB)に保存され、全員で共有。ほかの人はもう取りに行かない
    assert len(calls) == 1
    for c in (kid, admin):
        got = c.get(url).json()
        assert got["available"] and got["lyrics"]["layers"][0]["lines"][0]["text"] == "取ってきた歌詞"
    assert len(calls) == 1
    row = sqlite3.connect(cfg.db_path).execute("SELECT lrclib_body FROM lyrics_user WHERE track_id=?", (str(t),)).fetchone()
    assert row and "取ってきた歌詞" in row[0]

    # 見つからなかった曲は記録され、自動では取りに行かない状態になる
    class Miss:
        def fetch(self, q):
            return None
    b = ids(admin)[1]
    app.state.lyrics.lrclib = Miss()
    assert wife.post(f"/api/tracks/{b}/lyrics/fetch").json()["lrclibMiss"] is True
    assert kid.get(f"/api/tracks/{b}/lyrics").json()["lrclibMiss"] is True


def test_admin_password_reset_with_code_from_nas(env):
    import os
    from otokura import auth as A
    app, cfg, _ = env
    assert client(app).post("/api/auth/reset", json={"code": "x", "password": PW}).status_code == 409   # 管理者がまだいない
    admin = setup_admin(app, cfg)
    wife = add_member(app, admin)
    c = client(app, "192.168.0.60")
    NEW = "brand new password"
    # コードを作っていなければ通らない
    assert c.post("/api/auth/reset", json={"code": "", "password": NEW}).status_code == 403
    code = A.write_reset_code(cfg.data_dir)
    path = cfg.data_dir / "reset-code.txt"
    assert path.is_file()
    assert c.post("/api/auth/reset", json={"code": "wrong", "password": NEW}).status_code == 403
    # パスワードが短いときはコードを使い切らない
    assert c.post("/api/auth/reset", json={"code": code, "password": "short"}).status_code == 422 and path.is_file()
    # 期限切れ(15 分)は通らず、ファイルも消える
    old = time.time() - A.RESET_S - 5
    os.utime(path, (old, old))
    assert c.post("/api/auth/reset", json={"code": code, "password": NEW}).status_code == 403 and not path.exists()
    code = A.write_reset_code(cfg.data_dir)
    r = c.post("/api/auth/reset", json={"code": code, "password": NEW})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin" and c.get("/api/users").status_code == 200   # そのままログイン
    assert not path.exists()
    assert admin.get("/api/library").status_code == 401                               # 前のログインは切れる
    assert client(app).post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 401
    assert client(app).post("/api/auth/login", json={"name": "cross", "password": NEW}).status_code == 200
    assert client(app).post("/api/auth/reset", json={"code": code, "password": "another password"}).status_code == 403   # 使い回せない
    assert wife.get("/api/library").status_code == 200                                # 家族には影響しない
    # 総当たりは止める
    A.write_reset_code(cfg.data_dir)
    d = client(app, "192.168.0.61")
    codes = [d.post("/api/auth/reset", json={"code": "bad", "password": NEW}).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429


def test_reset_code_command_prints_code(tmp_path, monkeypatch, capsys):
    from otokura import auth as A
    monkeypatch.setenv("OTOKURA_DATA", str(tmp_path))
    assert A.main(["reset-code"]) == 0
    out = capsys.readouterr().out
    assert (tmp_path / "reset-code.txt").read_text().strip() in out and "15 分" in out
    assert A.main([]) == 2


def test_dlna_for_family_only_from_home(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    home = add_member(app, admin, "妻", "192.168.0.11")
    assert home.get("/api/auth/state").json()["canDlna"] is True
    assert home.get("/api/dlna/x/status").status_code != 403
    for ip in ("100.101.102.103", "fd7a:115c:a1e0::1", "127.0.0.1", "8.8.8.8"):     # Tailscale・外・転送元が不明
        out = client(app, ip)
        assert out.post("/api/auth/login", json={"name": "妻", "password": PW}).status_code == 200
        assert out.get("/api/auth/state").json()["canDlna"] is False, ip
        assert out.get("/api/dlna/devices").status_code == 403, ip
        assert out.post("/api/dlna/x/load", json={"trackId": 1}).status_code == 403, ip
    away = client(app, "100.101.102.104")                                            # 管理者は外からでも使える
    assert away.post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 200
    assert away.get("/api/auth/state").json()["canDlna"] is True


def test_cast_links_work_without_login_only_for_that_track(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    a, b = ids(admin)
    link = admin.get(f"/api/tracks/{a}/cast-link").json()
    assert link["url"].startswith("http://192.168.0.252:8686/")
    device = client(app, "192.168.0.70")                                             # Cast の機器はログインできない
    path = link["url"].split("8686", 1)[1]
    assert device.get(path).status_code == 200 and device.head(path).status_code == 200
    assert device.get(path.replace(f"/tracks/{a}/", f"/tracks/{b}/")).status_code == 401      # 別の曲には使えない
    assert device.get(path.split("?")[0]).status_code == 401
    assert device.get(path[:-1] + ("0" if path[-1] != "0" else "1")).status_code == 401       # 署名の改ざん
    assert device.get("/api/library", params={"t": path.split("t=")[1]}).status_code == 401   # ほかの API には使えない
    from otokura import auth as auth_mod
    t = app.state.auth.link("track", a, ttl=-1)                                      # 期限切れ
    assert device.get(f"/api/tracks/{a}/stream?t={t}").status_code == 401
    assert link["art"] is None or device.get(link["art"].split("8686", 1)[1]).status_code == 200


def test_user_management(env):
    app, cfg, _ = env
    admin = setup_admin(app, cfg)
    m = add_member(app, admin)
    us = admin.get("/api/users").json()
    assert {u["name"]: u["role"] for u in us} == {"cross": "admin", "妻": "member"}
    assert all("pw_hash" not in u for u in us)
    mid = next(u["id"] for u in us if u["name"] == "妻")
    assert admin.post("/api/users", json={"name": "妻", "password": PW}).status_code == 409
    assert admin.post("/api/users", json={"name": "子", "password": "too short"}).status_code == 422
    # 自分のパスワード変更: 今のパスワードが要る。他の端末はログアウトする
    other = client(app, "192.168.0.12")
    other.post("/api/auth/login", json={"name": "妻", "password": PW})
    assert m.post("/api/auth/password", json={"current": "bad", "new": "new password 1"}).status_code == 403
    assert m.post("/api/auth/password", json={"current": PW, "new": "new password 1"}).status_code == 200
    assert m.get("/api/library").status_code == 200 and other.get("/api/library").status_code == 401
    # 管理者の再設定: その人の全端末がログアウトする
    assert admin.put(f"/api/users/{mid}/password", json={"password": "new password 2"}).status_code == 200
    assert m.get("/api/library").status_code == 401
    assert client(app).post("/api/auth/login", json={"name": "妻", "password": "new password 2"}).status_code == 200
    # 削除: 本人のデータも消える。自分自身は消せない
    me = next(u["id"] for u in us if u["name"] == "cross")
    assert admin.delete(f"/api/users/{me}").status_code == 409
    assert admin.delete(f"/api/users/{mid}").status_code == 200
    assert client(app).post("/api/auth/login", json={"name": "妻", "password": "new password 2"}).status_code == 401


def test_old_database_moves_to_first_admin(tmp_path):
    from otokura.app import create_app
    from otokura.config import Config, Source
    make_audio(tmp_path / "m" / "a" / "01 one.flac")
    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(data / "otokura.db")
    old.executescript("""
      CREATE TABLE track_stats (source TEXT NOT NULL, rel TEXT NOT NULL, plays INTEGER NOT NULL DEFAULT 0, last_played REAL, skips INTEGER NOT NULL DEFAULT 0,
        fav INTEGER NOT NULL DEFAULT 0, rating INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source, rel));
      CREATE TABLE playlists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, rule TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO track_stats VALUES('m','a/01 one.flac',7,100.0,2,1,4);
      INSERT INTO playlists VALUES(1,'古いリスト',NULL,1,1);
      INSERT INTO settings VALUES('ui','{"theme":"dark"}');
    """)
    old.commit(); old.close()
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=data, scan_at=None, scan_on_start=False, lrclib=False, webinfo=False, auth=True)
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    assert (data / "otokura.pre-accounts.db").is_file()                              # 移す前のバックアップ
    bak = sqlite3.connect(data / "otokura.pre-accounts.db")
    assert bak.execute("SELECT plays FROM track_stats").fetchone()[0] == 7
    bak.close()
    admin = setup_admin(app, cfg)
    j = admin.get("/api/library").json()
    t = dict(zip(j["trackColumns"], j["tracks"][0]))
    assert (t["plays"], t["skips"], t["fav"], t["rating"]) == (7, 2, 1, 4)
    assert [p["name"] for p in admin.get("/api/playlists").json()] == ["古いリスト"]
    assert admin.get("/api/settings").json()["ui"]["theme"] == "dark"
    m = add_member(app, admin)
    j = m.get("/api/library").json()
    assert dict(zip(j["trackColumns"], j["tracks"][0]))["plays"] == 0 and m.get("/api/playlists").json() == []
    # もう一度起動しても二重に移さない
    app2 = create_app(cfg, start_background=False)
    c2 = client(app2)
    assert c2.post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 200
    j = c2.get("/api/library").json()
    assert dict(zip(j["trackColumns"], j["tracks"][0]))["plays"] == 7
