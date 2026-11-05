"""指紋・PIN のログイン(パスキー)と、スマホで承認するテレビのログイン。

- パスキーは端末ごとに登録する。指紋や PIN は端末の中で確かめられ、音蔵には届かない。DB には公開鍵だけを置く
- サイトの名前はアクセスされたアドレスから取る。ブラウザが別のサイトの鍵を渡さないので、偽のサイトには使えない
- https のアドレス(または試験用の localhost)でだけ使える。IP アドレスの http ではブラウザが使わせない
- テレビはコードを出すだけ。ログイン済みのスマホが指紋で承認すると、テレビを待っている id にログインを渡す
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import threading
import time

from fastapi import HTTPException, Request
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

log = logging.getLogger("otokura.passkey")

CHALLENGE_S = 5 * 60
DEVICE_S = 5 * 60
MAX_CHALLENGES = 1000
MAX_DEVICES_PER_IP = 20
MAX_DEVICES = 200
MAX_FAILS_PER_IP = 20
MAX_CODE_FAILS = 5
_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"    # 見間違えやすい 0 O 1 I L は使わない


def _site(request: Request) -> tuple[str, str]:
    u = request.url
    host = u.hostname or ""
    try:
        ipaddress.ip_address(host)
        usable = False
    except ValueError:
        usable = u.scheme == "https" or host == "localhost"
    if not usable:
        raise HTTPException(409, "指紋のログインは https のアドレスで開いたときに使えます")
    return host, f"{u.scheme}://{u.netloc}"


def _label(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    for key, name in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux")):
        if key in ua:
            return name
    return "端末"


def _code_key(code: str) -> str:
    return "".join(c for c in str(code or "").upper() if c.isalnum())


class Passkeys:
    def __init__(self, auth):
        self.auth, self.db = auth, auth.db
        self._lock = threading.Lock()
        self._challenges: dict[str, dict] = {}
        self._devices: dict[str, dict] = {}

    # ---------- 確認の問題(1 回限り) ----------
    def _put(self, kind: str, challenge: bytes, site: tuple[str, str], uid: int | None) -> str:
        cid, now = secrets.token_urlsafe(24), time.time()
        with self._lock:
            self._challenges = {k: v for k, v in self._challenges.items() if v["until"] > now}
            if len(self._challenges) >= MAX_CHALLENGES:
                raise HTTPException(429, "混み合っています。少し待ってからもう一度試してください")
            self._challenges[cid] = {"kind": kind, "challenge": challenge, "site": site, "uid": uid, "until": now + CHALLENGE_S}
        return cid

    def _take(self, cid: str, kind: str) -> dict | None:
        with self._lock:
            c = self._challenges.pop(str(cid or ""), None)
        return c if c and c["kind"] == kind and c["until"] > time.time() else None

    # ---------- 登録 ----------
    def list(self, uid: int) -> list[dict]:
        return [{"id": r["id"], "label": r["label"], "createdAt": r["created_at"]}
                for r in self.db.q("SELECT * FROM passkeys WHERE user_id=? ORDER BY created_at", (uid,))]

    def register_start(self, request: Request, user: dict, password: str) -> dict:
        site = _site(request)
        self.auth._confirm_self(user["id"], password)
        o = generate_registration_options(
            rp_id=site[0], rp_name="音蔵", user_id=str(user["id"]).encode(), user_name=user["name"],
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                                   user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in self.list(user["id"])])
        return {"challengeId": self._put("register", o.challenge, site, user["id"]), "options": json.loads(options_to_json(o))}

    def register_finish(self, request: Request, user: dict, challenge_id: str, credential: dict) -> None:
        c = self._take(challenge_id, "register")
        if not c or c["uid"] != user["id"]:
            raise HTTPException(403, "時間が経ちました。もう一度お試しください")
        try:
            v = verify_registration_response(credential=credential, expected_challenge=c["challenge"], expected_rp_id=c["site"][0],
                                             expected_origin=c["site"][1], require_user_verification=True)
        except Exception as e:      # 形が違うものも、署名が合わないものも、すべて断る
            log.info("パスキーの登録を断りました: %s", e)
            raise HTTPException(403, "指紋を確かめられませんでした") from None
        with self.db.write_lock:
            self.db.conn.execute("INSERT OR REPLACE INTO passkeys(id,user_id,public_key,sign_count,label,created_at) VALUES(?,?,?,?,?,?)",
                                 (bytes_to_base64url(v.credential_id), user["id"], v.credential_public_key, v.sign_count, _label(request), time.time()))
            self.db.conn.commit()

    def remove(self, user: dict, pid: str, password: str) -> None:
        self.auth._confirm_self(user["id"], password)
        with self.db.write_lock:
            n = self.db.conn.execute("DELETE FROM passkeys WHERE id=? AND user_id=?", (str(pid or ""), user["id"])).rowcount
            self.db.conn.commit()
        if not n:
            raise HTTPException(404, "見つかりません")

    def clear(self, uid: int) -> None:
        with self.db.write_lock:
            self.db.conn.execute("DELETE FROM passkeys WHERE user_id=?", (uid,))
            self.db.conn.commit()

    def count(self, uid: int) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM passkeys WHERE user_id=?", (uid,))["n"]

    # ---------- 指紋で確かめる ----------
    def _verify(self, c: dict, credential: dict, uid: int | None) -> int | None:
        """合っていれば持ち主の id を返す。uid を渡すと、その人の鍵だけを受ける。"""
        row = self.db.one("SELECT * FROM passkeys WHERE id=?", (str((credential or {}).get("id") or ""),))
        if not row or (uid is not None and row["user_id"] != uid):
            return None
        try:
            v = verify_authentication_response(credential=credential, expected_challenge=c["challenge"], expected_rp_id=c["site"][0], expected_origin=c["site"][1],
                                               credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"], require_user_verification=True)
        except Exception as e:
            log.info("パスキーの確認を断りました: %s", e)
            return None
        with self.db.write_lock:
            self.db.conn.execute("UPDATE passkeys SET sign_count=? WHERE id=?", (v.new_sign_count, row["id"]))
            self.db.conn.commit()
        return row["user_id"]

    def _options(self, kind: str, site: tuple[str, str], uid: int | None) -> dict:
        allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in self.list(uid)] if uid is not None else None
        o = generate_authentication_options(rp_id=site[0], allow_credentials=allow, user_verification=UserVerificationRequirement.REQUIRED)
        return {"challengeId": self._put(kind, o.challenge, site, uid), "options": json.loads(options_to_json(o))}

    def login_start(self, request: Request) -> dict:
        return self._options("login", _site(request), None)

    def login_finish(self, request: Request, challenge_id: str, credential: dict) -> int:
        key = ("#passkey", request.client.host if request.client else "")
        wait = self.auth._throttled(key, MAX_FAILS_PER_IP)
        if wait:
            raise HTTPException(429, f"続けて失敗したので、{int(wait) + 1} 秒待ってください")
        c = self._take(challenge_id, "login")
        uid = self._verify(c, credential, None) if c else None
        if uid is None:
            self.auth._failed(key, MAX_FAILS_PER_IP)
            raise HTTPException(401, "指紋を確かめられませんでした。この端末で登録していなければ、パスワードでログインしてください")
        return uid

    def confirm_start(self, request: Request, user: dict) -> dict:
        return self._options("confirm", _site(request), user["id"])

    # ---------- テレビのログイン ----------
    def device_start(self, request: Request) -> dict:
        ip, now = request.client.host if request.client else "", time.time()
        did = secrets.token_urlsafe(32)
        code = "".join(secrets.choice(_CODE_CHARS) for _ in range(8))
        with self._lock:
            self._devices = {k: v for k, v in self._devices.items() if v["until"] > now}
            if len(self._devices) >= MAX_DEVICES or sum(v["ip"] == ip for v in self._devices.values()) >= MAX_DEVICES_PER_IP:
                raise HTTPException(429, "混み合っています。少し待ってからもう一度試してください")
            self._devices[did] = {"code": code, "ip": ip, "uid": None, "until": now + DEVICE_S}
        return {"id": did, "code": f"{code[:4]}-{code[4:]}", "expiresIn": DEVICE_S}

    def device_poll(self, did: str) -> int | None:
        """まだなら None。承認されていれば持ち主の id を返し、この id はもう使えなくする。"""
        now = time.time()
        with self._lock:
            d = self._devices.get(str(did or ""))
            if not d or d["until"] <= now:
                self._devices.pop(str(did or ""), None)
                raise HTTPException(404, "コードの期限が切れました")
            if d["uid"] is None:
                return None
            del self._devices[str(did)]
            return d["uid"]

    def device_approve(self, user: dict, body: dict) -> None:
        if user.get("mustChange") or user.get("needsSecond"):
            raise HTTPException(403, "先にこの端末のログインの設定を済ませてください")
        key = ("#device", str(user["id"]))
        wait = self.auth._throttled(key, MAX_CODE_FAILS)
        if wait:
            raise HTTPException(429, f"続けて間違えたので、{int(wait) + 1} 秒待ってください")
        code, now = _code_key(body.get("code")), time.time()
        with self._lock:
            target = next((d for d in self._devices.values() if d["code"] == code and d["uid"] is None and d["until"] > now), None)
        if target is None:
            self.auth._failed(key, MAX_CODE_FAILS)
            raise HTTPException(404, "コードが違うか、期限が切れています。テレビの画面を確かめてください")
        self._prove(user, body)
        with self._lock:
            target["uid"] = user["id"]
        with self.auth._lock:
            self.auth._fails.pop(key, None)

    def _prove(self, user: dict, body: dict) -> None:
        """承認する本人の確認。指紋があれば指紋。管理者はパスワードだけでは通さない。"""
        if body.get("credential"):
            c = self._take(body.get("challengeId"), "confirm")
            if not c or c["uid"] != user["id"] or self._verify(c, body["credential"], user["id"]) is None:
                raise HTTPException(403, "指紋を確かめられませんでした")
            return
        row = self.db.one("SELECT totp_secret,role FROM users WHERE id=?", (user["id"],))
        if row["role"] == "admin" and not row["totp_secret"]:
            raise HTTPException(403, "管理者は指紋で承認してください")
        self.auth._confirm_self(user["id"], body.get("password"))
        if row["totp_secret"]:
            self.auth.check_code(user["id"], body.get("totp"))
