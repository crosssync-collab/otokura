"""最初のパスワード変更の必須化と、二重認証(認証アプリの 6 桁のコード)。"""
import time

import pytest

from test_auth import PW, client, env, setup_admin_raw  # noqa: F401

NEW = "another good phrase"
MAIL = "taro.example@gmail.com"


def code(secret, shift=0):
    from otokura.auth import TOTP_STEP, totp_code
    return totp_code(secret, int(time.time()) // TOTP_STEP + shift)


def enroll(c, password=PW):
    r = c.post("/api/auth/totp/start", json={"password": password})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert c.post("/api/auth/totp/confirm", json={"code": code(secret)}).status_code == 200
    return secret


def state(c):
    return c.get("/api/auth/state").json()["user"]


def test_totp_matches_the_rfc_vectors():
    import base64
    from otokura.auth import totp_code
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_code(secret, 0) == "755224"             # RFC 4226 付録 D
    assert totp_code(secret, 1) == "287082"             # RFC 6238 付録 B(59 秒)の下 6 桁
    assert totp_code(secret, 37037036) == "081804"      # RFC 6238 付録 B(1111111109 秒)


# ---------- 最初のパスワード変更 ----------
def member(app, admin, name=MAIL, password=None, ip="203.0.113.5"):
    assert admin.post("/api/users", json={"name": name, "password": password or name}).status_code == 200
    m = client(app, ip)
    assert m.post("/api/auth/login", json={"name": name, "password": password or name}).status_code == 200
    return m


@pytest.fixture
def admin(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    enroll(c)
    return c


def test_mail_address_can_be_the_initial_password(env, admin):
    m = member(env[0], admin)
    assert state(m)["mustChange"] is True and state(m)["role"] == "member"


def test_new_member_can_do_nothing_until_the_password_is_changed(env, admin):
    m = member(env[0], admin)
    for path in ("/api/library", "/api/playlists", "/api/tracks/1/stream", "/api/tracks/1/download", "/api/art/1"):
        assert m.get(path).status_code == 403, path
    assert m.post("/api/tracks/1/played").status_code == 403
    assert m.post("/api/auth/password", json={"current": MAIL, "new": NEW}).status_code == 200
    assert state(m)["mustChange"] is False
    assert m.get("/api/library").status_code == 200


def test_signed_links_are_not_a_way_around_the_change(env, admin):
    m = member(env[0], admin)
    assert m.get("/api/tracks/1/cast-link").status_code == 403


@pytest.mark.parametrize("bad", [MAIL, MAIL.upper(), "short"])
def test_new_password_must_be_really_new(env, admin, bad):
    m = member(env[0], admin)
    assert m.post("/api/auth/password", json={"current": MAIL, "new": bad}).status_code == 422
    assert state(m)["mustChange"] is True


def test_new_password_must_differ_from_the_name(env, admin):
    name = "hana.example@gmail.com"
    m = member(env[0], admin, name=name, password="temporary phrase")
    assert m.post("/api/auth/password", json={"current": "temporary phrase", "new": name}).status_code == 422


def test_wrong_current_password_is_refused_and_throttled(env, admin):
    m = member(env[0], admin)
    assert m.post("/api/auth/password", json={"current": "wrong wrong", "new": NEW}).status_code == 403
    for _ in range(5):
        r = m.post("/api/auth/password", json={"current": "wrong wrong", "new": NEW})
    assert r.status_code == 429


def test_admin_reset_of_a_member_means_change_again_and_clears_their_app(env, admin):
    app, _, _ = env
    m = member(app, admin)
    m.post("/api/auth/password", json={"current": MAIL, "new": NEW})
    enroll(m, NEW)
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["name"] == MAIL)
    assert admin.put(f"/api/users/{uid}/password", json={"password": "temporary phrase"}).status_code == 200
    again = client(app, "203.0.113.5")
    assert again.post("/api/auth/login", json={"name": MAIL, "password": "temporary phrase"}).status_code == 200    # コード無しで入れる(端末をなくした人の救済)
    u = state(again)
    assert u["mustChange"] is True and u["totp"] is False


def test_admin_cannot_reset_their_own_password_from_the_list(env, admin):
    me = state(admin)["id"]
    assert admin.put(f"/api/users/{me}/password", json={"password": "temporary phrase"}).status_code == 409


# ---------- 二重認証 ----------
def test_admin_must_set_up_the_app_before_anything_else(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    assert state(c)["needsSecond"] is True and state(c)["mustChange"] is False
    for path in ("/api/library", "/api/users", "/api/server"):
        assert c.get(path).status_code == 403, path
    assert c.post("/api/scan").status_code == 403
    enroll(c)
    assert state(c)["needsSecond"] is False and state(c)["totp"] is True
    assert c.get("/api/library").status_code == 200 and c.get("/api/users").status_code == 200


def test_start_needs_the_password_and_gives_a_qr_code(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    assert c.post("/api/auth/totp/start", json={"password": "wrong wrong"}).status_code == 403
    j = c.post("/api/auth/totp/start", json={"password": PW}).json()
    assert j["uri"].startswith("otpauth://totp/") and j["secret"] in j["uri"] and "issuer=Otokura" in j["uri"]
    assert j["qr"].lstrip().startswith("<svg")


def test_confirm_refuses_a_wrong_code_and_stays_pending(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = c.post("/api/auth/totp/start", json={"password": PW}).json()["secret"]
    assert c.post("/api/auth/totp/confirm", json={"code": "000000" if code(secret) != "000000" else "111111"}).status_code == 403
    assert state(c)["needsSecond"] is True
    assert c.post("/api/auth/totp/confirm", json={"code": code(secret)}).status_code == 200


def test_login_needs_the_code_after_the_password(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = enroll(c)
    o = client(app, "203.0.113.9")
    r = o.post("/api/auth/login", json={"name": "cross", "password": PW})
    assert r.status_code == 401 and r.headers["x-need-code"] == "1"
    assert state(o) is None and o.get("/api/library").status_code == 401
    r = o.post("/api/auth/login", json={"name": "cross", "password": "wrong wrong"})
    assert r.status_code == 401 and "x-need-code" not in r.headers       # パスワードが違うときはコードの話をしない
    r = o.post("/api/auth/login", json={"name": "cross", "password": PW, "code": code(secret, 1)})
    assert r.status_code == 200 and o.get("/api/library").status_code == 200


def test_code_cannot_be_used_twice(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = enroll(c)
    a, b = client(app, "203.0.113.9"), client(app, "203.0.113.10")
    used = code(secret, 1)
    assert a.post("/api/auth/login", json={"name": "cross", "password": PW, "code": used}).status_code == 200
    assert b.post("/api/auth/login", json={"name": "cross", "password": PW, "code": used}).status_code == 401


def test_old_codes_are_refused(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = enroll(c)
    o = client(app, "203.0.113.9")
    assert o.post("/api/auth/login", json={"name": "cross", "password": PW, "code": code(secret, -3)}).status_code == 401


def test_wrong_codes_are_throttled(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = enroll(c)
    o = client(app, "203.0.113.9")
    bad = "000000" if code(secret) != "000000" else "111111"
    for _ in range(5):
        assert o.post("/api/auth/login", json={"name": "cross", "password": PW, "code": bad}).status_code == 401
    assert o.post("/api/auth/login", json={"name": "cross", "password": PW, "code": code(secret, 1)}).status_code == 429


def test_setting_up_the_app_logs_out_other_devices(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    other = client(app, "203.0.113.9")
    assert other.post("/api/auth/login", json={"name": "cross", "password": PW}).status_code == 200
    enroll(c)
    assert state(other) is None and state(c) is not None


def test_member_can_choose_the_app_and_is_not_forced(env, admin):
    app, _, _ = env
    m = member(app, admin)
    m.post("/api/auth/password", json={"current": MAIL, "new": NEW})
    assert state(m)["needsSecond"] is False and state(m)["totp"] is False
    assert m.get("/api/library").status_code == 200
    secret = enroll(m, NEW)
    o = client(app, "203.0.113.20")
    assert o.post("/api/auth/login", json={"name": MAIL, "password": NEW}).headers["x-need-code"] == "1"
    assert m.post("/api/auth/totp/off", json={"password": "wrong wrong", "code": code(secret, 1)}).status_code == 403
    assert m.post("/api/auth/totp/off", json={"password": NEW, "code": code(secret, 1)}).status_code == 200
    assert o.post("/api/auth/login", json={"name": MAIL, "password": NEW}).status_code == 200


def test_admin_who_turns_it_off_must_set_it_up_again(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = enroll(c)
    assert c.post("/api/auth/totp/off", json={"password": PW, "code": code(secret, 1)}).status_code == 200
    assert state(c)["needsSecond"] is True and c.get("/api/library").status_code == 403


def test_reset_code_clears_the_app_for_a_lost_phone(env):
    from otokura.auth import write_reset_code
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    enroll(c)
    rc = write_reset_code(cfg.data_dir)
    o = client(app, "203.0.113.9")
    r = o.post("/api/auth/reset", json={"code": rc, "password": NEW})
    assert r.status_code == 200 and r.json()["user"]["needsSecond"] is True and r.json()["user"]["mustChange"] is False
    assert o.get("/api/library").status_code == 403
    enroll(o, NEW)
    assert o.get("/api/library").status_code == 200


def test_secret_never_leaves_after_setup(env):
    app, cfg, _ = env
    c = setup_admin_raw(app, cfg)
    secret = enroll(c)
    assert secret not in c.get("/api/auth/state").text and secret not in c.get("/api/users").text
    assert c.post("/api/auth/totp/start", json={"password": PW}).status_code == 409


def test_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    from otokura.db import Database
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,pw_hash TEXT NOT NULL,role TEXT NOT NULL,created_at REAL NOT NULL);"
                    "INSERT INTO users VALUES(1,'cross','x','admin',1);")
    c.commit(); c.close()
    d = Database(p)
    r = d.one("SELECT * FROM users WHERE id=1")
    assert r["must_change"] == 0 and r["totp_secret"] is None and r["name"] == "cross"
