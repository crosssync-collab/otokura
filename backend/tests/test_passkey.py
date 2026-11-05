"""指紋・PIN のログイン(パスキー)と、スマホで承認するテレビのログイン。"""
import base64
import hashlib
import json
import os
import time

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from test_auth import PW, env  # noqa: F401
from test_two_factor import NEW

BASE = "https://music.example:8443"
ORIGIN = BASE
RP = "music.example"


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Phone:
    """本物と同じ形の応答を返す、試験用の認証器。"""

    def __init__(self, rp=RP, origin=ORIGIN):
        self.key, self.cid, self.rp, self.origin, self.count = ec.generate_private_key(ec.SECP256R1()), os.urandom(32), rp, origin, 0

    def _client(self, kind, challenge):
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options):
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        data = hashlib.sha256(self.rp.encode()).digest() + b"\x45" + (0).to_bytes(4, "big") + b"\0" * 16 + len(self.cid).to_bytes(2, "big") + self.cid + cose
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
        return {"id": b64(self.cid), "rawId": b64(self.cid), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(self._client("webauthn.create", options["challenge"])), "attestationObject": b64(att)}}

    def get(self, options, flags=0x05):
        self.count += 1
        data = hashlib.sha256(self.rp.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
        client = self._client("webauthn.get", options["challenge"])
        sig = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cid), "rawId": b64(self.cid), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(data), "signature": b64(sig)}}


def client(app, ip="203.0.113.5", base=BASE):
    return TestClient(app, base_url=base, client=(ip, 50000))


def register(c, phone, password=PW):
    r = c.post("/api/auth/passkey/register/start", json={"password": password})
    assert r.status_code == 200, r.text
    j = r.json()
    r = c.post("/api/auth/passkey/register/finish", json={"challengeId": j["challengeId"], "credential": phone.create(j["options"])})
    assert r.status_code == 200, r.text


def login(c, phone, **kw):
    j = c.post("/api/auth/passkey/login/start").json()
    return c.post("/api/auth/passkey/login/finish", json={"challengeId": j["challengeId"], "credential": phone.get(j["options"], **kw)})


def state(c):
    return c.get("/api/auth/state").json()["user"]


@pytest.fixture
def raw_admin(env):
    app, cfg, _ = env
    c = client(app, "198.51.100.1")
    code = (cfg.data_dir / "setup-code.txt").read_text().strip()
    assert c.post("/api/auth/setup", json={"code": code, "name": "cross", "password": PW}).status_code == 200
    return c


@pytest.fixture
def admin(raw_admin):
    raw_admin.phone = Phone()
    register(raw_admin, raw_admin.phone)
    return raw_admin


def member(app, admin, name="taro@example.com", ip="203.0.113.7"):
    assert admin.post("/api/users", json={"name": name, "password": name}).status_code == 200
    m = client(app, ip)
    assert m.post("/api/auth/login", json={"name": name, "password": name}).status_code == 200
    assert m.post("/api/auth/password", json={"current": name, "new": NEW}).status_code == 200
    return m


# ---------- 指紋の登録 ----------
def test_admin_must_register_before_anything_else(raw_admin):
    assert state(raw_admin)["needsSecond"] is True
    assert raw_admin.get("/api/library").status_code == 403
    register(raw_admin, Phone())
    u = state(raw_admin)
    assert u["needsSecond"] is False and u["passkeys"] == 1
    assert raw_admin.get("/api/library").status_code == 200


def test_registration_needs_the_password(raw_admin):
    assert raw_admin.post("/api/auth/passkey/register/start", json={"password": "wrong wrong"}).status_code == 403


def test_registration_asks_for_a_fingerprint_on_this_site(raw_admin):
    j = raw_admin.post("/api/auth/passkey/register/start", json={"password": PW}).json()["options"]
    assert j["rp"]["id"] == RP and j["authenticatorSelection"]["userVerification"] == "required"
    assert j["authenticatorSelection"]["residentKey"] == "required" and j["user"]["name"] == "cross"


@pytest.mark.parametrize("phone", [Phone(rp="evil.example"), Phone(origin="https://evil.example")])
def test_registration_from_another_site_is_refused(raw_admin, phone):
    j = raw_admin.post("/api/auth/passkey/register/start", json={"password": PW}).json()
    r = raw_admin.post("/api/auth/passkey/register/finish", json={"challengeId": j["challengeId"], "credential": phone.create(j["options"])})
    assert r.status_code == 403 and state(raw_admin)["needsSecond"] is True


@pytest.mark.parametrize("base", ["http://192.168.0.252:8686", "http://music.example:8686", "https://192.168.0.252:8443"])
def test_unusable_addresses_are_refused_with_a_reason(env, base):
    app, cfg, _ = env
    c = client(app, "192.168.0.10", base=base)
    code = (cfg.data_dir / "setup-code.txt").read_text().strip()
    assert c.post("/api/auth/setup", json={"code": code, "name": "cross", "password": PW}).status_code == 200
    r = c.post("/api/auth/passkey/register/start", json={"password": PW})
    assert r.status_code == 409 and "https" in r.json()["detail"]
    assert c.post("/api/auth/passkey/login/start").status_code == 409


def test_localhost_is_allowed_for_trying_out(env):
    app, cfg, _ = env
    c = client(app, "127.0.0.1", base="http://localhost:8686")
    code = (cfg.data_dir / "setup-code.txt").read_text().strip()
    assert c.post("/api/auth/setup", json={"code": code, "name": "cross", "password": PW}).status_code == 200
    assert c.post("/api/auth/passkey/register/start", json={"password": PW}).status_code == 200


# ---------- 指紋でログイン ----------
def test_login_with_a_fingerprint_needs_no_password(env, admin):
    c = client(env[0])
    r = login(c, admin.phone)
    assert r.status_code == 200 and r.json()["user"]["name"] == "cross"
    assert c.get("/api/users").status_code == 200


def test_password_alone_is_not_enough_for_the_admin(env, admin):
    c = client(env[0])
    r = c.post("/api/auth/login", json={"name": "cross", "password": PW})
    assert r.status_code == 401 and r.headers["x-need-passkey"] == "1"
    assert state(c) is None
    r = c.post("/api/auth/login", json={"name": "cross", "password": "wrong wrong"})
    assert r.status_code == 401 and "x-need-passkey" not in r.headers    # パスワードが違うときは指紋の話をしない


def test_fingerprint_must_really_be_checked(env, admin):
    assert login(client(env[0]), admin.phone, flags=0x01).status_code == 401    # 触れただけ(指紋の確認なし)は通さない


def test_challenge_is_single_use(env, admin):
    c = client(env[0])
    j = c.post("/api/auth/passkey/login/start").json()
    cred = admin.phone.get(j["options"])
    assert c.post("/api/auth/passkey/login/finish", json={"challengeId": j["challengeId"], "credential": cred}).status_code == 200
    assert client(env[0]).post("/api/auth/passkey/login/finish", json={"challengeId": j["challengeId"], "credential": cred}).status_code == 401


def test_challenge_expires(env, admin, monkeypatch):
    c = client(env[0])
    j = c.post("/api/auth/passkey/login/start").json()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 3600)
    assert c.post("/api/auth/passkey/login/finish", json={"challengeId": j["challengeId"], "credential": admin.phone.get(j["options"])}).status_code == 401


def test_unknown_key_is_refused_and_throttled(env, admin):
    c = client(env[0], "203.0.113.9")
    stranger = Phone()
    codes = [login(c, stranger).status_code for _ in range(21)]
    assert codes[0] == 401 and codes[-1] == 429
    assert login(client(env[0], "203.0.113.10"), admin.phone).status_code == 200     # 別の接続元は巻き込まない


def test_signature_from_another_key_is_refused(env, admin):
    forged = Phone()
    forged.cid = admin.phone.cid
    assert login(client(env[0]), forged).status_code == 401


def test_each_device_registers_its_own(env, admin):
    pc = Phone()
    r = admin.post("/api/auth/passkey/register/start", json={"password": PW}).json()
    assert [x["id"] for x in r["options"]["excludeCredentials"]] == [b64(admin.phone.cid)]
    assert admin.post("/api/auth/passkey/register/finish", json={"challengeId": r["challengeId"], "credential": pc.create(r["options"])}).status_code == 200
    assert login(client(env[0]), pc).status_code == 200 and login(client(env[0]), admin.phone).status_code == 200
    assert len(admin.get("/api/auth/passkeys").json()) == 2


def test_list_shows_device_kind_not_secrets(env):
    app, cfg, _ = env
    c = TestClient(app, base_url=BASE, client=("198.51.100.1", 50000), headers={"User-Agent": "Mozilla/5.0 (Linux; Android 16) Chrome/140"})
    code = (cfg.data_dir / "setup-code.txt").read_text().strip()
    c.post("/api/auth/setup", json={"code": code, "name": "cross", "password": PW})
    register(c, Phone())
    l = c.get("/api/auth/passkeys").json()
    assert l[0]["label"] == "Android" and "public_key" not in l[0] and "publicKey" not in l[0]


def test_removing_the_last_one_means_register_again(env, admin):
    pid = admin.get("/api/auth/passkeys").json()[0]["id"]
    assert admin.post("/api/auth/passkey/remove", json={"id": pid, "password": "wrong wrong"}).status_code == 403
    assert admin.post("/api/auth/passkey/remove", json={"id": pid, "password": PW}).status_code == 200
    assert state(admin)["needsSecond"] is True and admin.get("/api/library").status_code == 403


def test_cannot_remove_someone_elses(env, admin):
    m = member(env[0], admin)
    pid = admin.get("/api/auth/passkeys").json()[0]["id"]
    assert m.post("/api/auth/passkey/remove", json={"id": pid, "password": NEW}).status_code == 404
    assert len(admin.get("/api/auth/passkeys").json()) == 1


def test_admin_login_with_app_is_still_a_fallback(env, admin):
    from test_two_factor import code as totp
    s = admin.post("/api/auth/totp/start", json={"password": PW}).json()["secret"]
    assert admin.post("/api/auth/totp/confirm", json={"code": totp(s)}).status_code == 200
    c = client(env[0], "203.0.113.30")
    assert c.post("/api/auth/login", json={"name": "cross", "password": PW}).headers["x-need-code"] == "1"
    assert c.post("/api/auth/login", json={"name": "cross", "password": PW, "code": totp(s, 1)}).status_code == 200


def test_reset_code_clears_fingerprints_for_a_lost_phone(env, admin):
    from otokura.auth import write_reset_code
    rc = write_reset_code(env[1].data_dir)
    c = client(env[0], "203.0.113.31")
    r = c.post("/api/auth/reset", json={"code": rc, "password": NEW})
    assert r.status_code == 200 and r.json()["user"]["needsSecond"] is True and r.json()["user"]["passkeys"] == 0
    assert login(client(env[0]), admin.phone).status_code == 401


def test_family_can_choose_and_keep_the_password_too(env, admin):
    m = member(env[0], admin)
    assert state(m)["needsSecond"] is False
    p = Phone()
    register(m, p, NEW)
    assert login(client(env[0]), p).json()["user"]["name"] == "taro@example.com"
    assert client(env[0]).post("/api/auth/login", json={"name": "taro@example.com", "password": NEW}).status_code == 200


def test_admin_reset_clears_a_members_fingerprints(env, admin):
    m = member(env[0], admin)
    p = Phone()
    register(m, p, NEW)
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["name"] == "taro@example.com")
    assert admin.put(f"/api/users/{uid}/password", json={"password": "temporary phrase"}).status_code == 200
    assert login(client(env[0]), p).status_code == 401


def test_deleting_a_user_deletes_their_fingerprints(env, admin):
    m = member(env[0], admin)
    p = Phone()
    register(m, p, NEW)
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["name"] == "taro@example.com")
    assert admin.delete(f"/api/users/{uid}").status_code == 200
    assert login(client(env[0]), p).status_code == 401


# ---------- テレビのログイン ----------
def proof(c, phone):
    j = c.post("/api/auth/passkey/confirm/start").json()
    return {"challengeId": j["challengeId"], "credential": phone.get(j["options"])}


def tv(app):
    t = client(app, "192.168.0.50", base="http://192.168.0.252:8686")
    j = t.post("/api/auth/device/start").json()
    return t, j


def poll(t, j):
    return t.post("/api/auth/device/poll", json={"id": j["id"]})


def test_tv_logs_in_after_the_phones_fingerprint(env, admin):
    t, j = tv(env[0])
    assert len(j["code"].replace("-", "")) == 8 and state(t) is None
    assert poll(t, j).json()["status"] == "wait" and t.get("/api/library").status_code == 401
    r = admin.post("/api/auth/device/approve", json={"code": j["code"], **proof(admin, admin.phone)})
    assert r.status_code == 200, r.text
    r = poll(t, j)
    assert r.json()["status"] == "ok" and r.json()["user"]["name"] == "cross"
    assert t.get("/api/library").status_code == 200


def test_tv_code_is_forgiving_about_how_it_is_typed(env, admin):
    t, j = tv(env[0])
    typed = j["code"].replace("-", "").lower()
    assert admin.post("/api/auth/device/approve", json={"code": typed, **proof(admin, admin.phone)}).status_code == 200


def test_admin_cannot_approve_without_the_fingerprint(env, admin):
    t, j = tv(env[0])
    assert admin.post("/api/auth/device/approve", json={"code": j["code"]}).status_code == 403
    assert admin.post("/api/auth/device/approve", json={"code": j["code"], "password": PW}).status_code == 403
    assert poll(t, j).json()["status"] == "wait"


def test_only_your_own_fingerprint_counts(env, admin):
    m = member(env[0], admin)
    p = Phone()
    register(m, p, NEW)
    t, j = tv(env[0])
    assert admin.post("/api/auth/device/approve", json={"code": j["code"], **proof(admin, p)}).status_code == 403
    assert poll(t, j).json()["status"] == "wait"


def test_family_without_a_fingerprint_can_approve_with_the_password(env, admin):
    m = member(env[0], admin)
    t, j = tv(env[0])
    assert m.post("/api/auth/device/approve", json={"code": j["code"], "password": "wrong wrong"}).status_code == 403
    assert m.post("/api/auth/device/approve", json={"code": j["code"], "password": NEW}).status_code == 200
    assert poll(t, j).json()["user"]["name"] == "taro@example.com"


def test_approval_needs_a_login(env):
    t, j = tv(env[0])
    assert client(env[0]).post("/api/auth/device/approve", json={"code": j["code"], "password": PW}).status_code == 401


def test_person_who_must_change_the_password_cannot_approve(env, admin):
    admin.post("/api/users", json={"name": "hana@example.com", "password": "hana@example.com"})
    m = client(env[0], "203.0.113.8")
    m.post("/api/auth/login", json={"name": "hana@example.com", "password": "hana@example.com"})
    t, j = tv(env[0])
    assert m.post("/api/auth/device/approve", json={"code": j["code"], "password": "hana@example.com"}).status_code == 403


def test_tv_code_works_once_and_session_is_handed_over_once(env, admin):
    t, j = tv(env[0])
    assert admin.post("/api/auth/device/approve", json={"code": j["code"], **proof(admin, admin.phone)}).status_code == 200
    assert admin.post("/api/auth/device/approve", json={"code": j["code"], **proof(admin, admin.phone)}).status_code == 404
    assert poll(t, j).json()["status"] == "ok"
    other = client(env[0], "203.0.113.40")
    assert poll(other, j).status_code == 404 and other.get("/api/library").status_code == 401


def test_only_the_tv_holding_the_secret_id_gets_the_session(env, admin):
    t, j = tv(env[0])
    admin.post("/api/auth/device/approve", json={"code": j["code"], **proof(admin, admin.phone)})
    assert t.post("/api/auth/device/poll", json={"id": j["code"]}).status_code == 404


def test_tv_code_expires(env, admin, monkeypatch):
    t, j = tv(env[0])
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 3600)
    assert poll(t, j).status_code == 404
    assert admin.post("/api/auth/device/approve", json={"code": j["code"], "password": PW}).status_code in (403, 404)


def test_wrong_tv_codes_are_throttled(env, admin):
    m = member(env[0], admin)
    codes = [m.post("/api/auth/device/approve", json={"code": "AAAA-AAAA", "password": NEW}).status_code for _ in range(6)]
    assert codes[0] == 404 and codes[-1] == 429


def test_too_many_tv_codes_are_refused_from_one_place(env):
    c = client(env[0], "203.0.113.60")
    codes = [c.post("/api/auth/device/start").status_code for _ in range(25)]
    assert codes[0] == 200 and codes[-1] == 429
