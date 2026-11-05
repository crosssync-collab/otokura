"""音蔵のアカウント。家族に NAS の権限を渡さず、音蔵の中だけで人を分ける。

- 管理者(admin)と家族(member)の 2 種類。アカウントは音蔵の DB だけに置く(NAS のユーザーは増やさない)
- パスワードは scrypt。ログインは 1 年間そのまま(Cookie の値は DB にハッシュで置く)。続けて間違えると待ち時間を入れる
- 家族の書き込みは MEMBER_WRITES の許可リストだけ。新しい API は既定で管理者だけになる
- Cast・DLNA の機器はログインできないので、曲ごと・期限付きの署名を URL に付ける
- 管理者がパスワードを忘れたら、NAS で `docker exec otokura python -m otokura.auth reset-code` を実行して確認コードを作り、
  ログイン画面で再設定する。NAS に入れる人だけができる。家族のパスワードは管理者が画面で再設定する
- 管理者が作ったパスワードは本人が最初のログインで変える(must_change)。それまで API は使えない
- 管理者は指紋・PIN(パスキー、passkey.py)か認証アプリの 6 桁のコード(TOTP、RFC 6238)が必須で、パスワードだけでは入れない。家族は任意
- スマホをなくしたら、管理者は上の確認コードで、家族は管理者の再設定で、登録が消える
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import struct
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import segno
from fastapi import HTTPException, Request, Response

from .passkey import Passkeys

log = logging.getLogger("otokura.auth")

COOKIE = "otokura_s"
SESSION_S = 365 * 86400
LINK_S = 24 * 3600
MIN_PASSWORD = 10
MAX_FAILS = 5          # 同じ名前 + 同じ接続元
MAX_NAME_FAILS = 20    # 同じ名前(接続元を変えながらの攻撃)
MAX_IP_FAILS = 20      # 同じ接続元(名前を変えながらの攻撃)
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
RESET_S = 15 * 60
TOTP_STEP = 30
TOTP_ISSUER = "Otokura"
_TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))

# 家族に許す書き込み。ここに無いものは管理者だけ
MEMBER_WRITES = [re.compile(p) for p in (
    r"/api/tracks/\d+/(played|skipped|stats|prepare|loudness|lyrics/fetch)",
    r"/api/tracks/\d+/lyrics(/offset|/manual|/restore)?",    # 歌詞の編集はその人だけの修正になる(lyrics/api.py)
    r"/api/playlists(/\d+)?",
    r"/api/settings",
    r"/api/dlna/[^/]+/(load|play|pause|stop|seek|volume)",
)]
ADMIN_READS = [re.compile(r"/api/users(/.*)?")]
_LINKABLE = {"track": re.compile(r"/api/tracks/(\d+)/stream"), "art": re.compile(r"/api/art/(\d+)")}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), dklen=32, **SCRYPT)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password("otokura")     # いない名前でも同じ時間をかける


def totp_code(secret: str, step: int) -> str:
    d = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    o = d[-1] & 15
    return f"{(struct.unpack('>I', d[o:o + 4])[0] & 0x7fffffff) % 1000000:06d}"


def totp_match(secret: str, code: str, after: int = 0) -> int | None:
    """合っていれば時間の番号を返す。時計のずれを前後 1 つ分だけ許し、使ったコード(after 以前)は受けない。"""
    code = re.sub(r"\s", "", str(code or ""))
    now = int(time.time()) // TOTP_STEP
    for step in (now - 1, now, now + 1):
        if step > after and hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def is_home(request: Request) -> bool:
    """家の中のネットワークからの接続か。Tailscale・転送元が不明な接続は外として扱う。
    Tailscale の転送(Funnel を含む)を通るとヘッダーが付く。その中身は外からも書けるので信じない。"""
    if request.headers.get("x-forwarded-for"):
        return False
    try:
        a = ipaddress.ip_address(request.client.host)
    except (AttributeError, ValueError):
        return False
    return a.is_private and not a.is_loopback and not any(a in n for n in _TAILNET if n.version == a.version)


def _write_code(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    code = secrets.token_hex(5)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(code + "\n")
    return code


def write_reset_code(data_dir: Path) -> str:
    return _write_code(Path(data_dir) / "reset-code.txt")


class Auth:
    def __init__(self, db, data_dir: Path, enabled: bool = True):
        self.db, self.data_dir, self.enabled = db, Path(data_dir), enabled
        self._fails: dict[tuple[str, str], tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._hashing = threading.BoundedSemaphore(2)    # パスワードの照合は重いので同時に 2 つまで(大量の試行で NAS を止めない)
        self.passkeys = Passkeys(self)
        if enabled and not self.has_users():
            self._ensure_setup_code()

    # ---------- 最初の管理者 ----------
    @property
    def code_path(self) -> Path:
        return self.data_dir / "setup-code.txt"

    def has_users(self) -> bool:
        return self.db.one("SELECT 1 FROM users LIMIT 1") is not None

    def _ensure_setup_code(self) -> None:
        if self.code_path.is_file():
            return
        _write_code(self.code_path)
        log.warning("最初の管理者を登録してください。確認コードは %s にあります", self.code_path)

    def setup(self, code: str, name: str, password: str) -> int:
        if self.has_users():
            raise HTTPException(409, "管理者はすでに登録されています")
        try:
            real = self.code_path.read_text().strip()
        except OSError:
            real = ""
        if not real or not hmac.compare_digest(real.encode(), str(code).strip().encode()):
            raise HTTPException(403, "確認コードが違います")
        uid = self.create_user(name, password, "admin", uid=1)    # 今までの記録は id=1 に移してある
        self.code_path.unlink(missing_ok=True)
        return uid

    def reset_admin(self, request: Request, code: str, password: str) -> int:
        """NAS で作った確認コードで、管理者のパスワードを再設定する。その管理者の前のログインは切れる。"""
        row = self.db.one("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
        if row is None:
            raise HTTPException(409, "管理者がまだ登録されていません")
        key = ("#reset", request.client.host if request.client else "")
        wait = self._throttled(key)
        if wait:
            raise HTTPException(429, f"続けて間違えたので、{int(wait) + 1} 秒待ってください")
        path = self.data_dir / "reset-code.txt"
        try:
            real, age = path.read_text().strip(), time.time() - path.stat().st_mtime
        except OSError:
            real, age = "", 0
        if real and age > RESET_S:
            path.unlink(missing_ok=True)
            real = ""
        if not real or not hmac.compare_digest(real.encode(), str(code).strip().encode()):
            self._failed(key)
            raise HTTPException(403, "確認コードが違うか、期限(15 分)が切れています")
        name = self.db.one("SELECT name FROM users WHERE id=?", (row["id"],))["name"]
        self._not_weak(name, password)            # 短いパスワードならここで止まり、コードは使い切らない
        self.set_password(row["id"], password)
        self._clear_second(row["id"])             # スマホをなくしたときの出口。次の画面で登録し直す
        path.unlink(missing_ok=True)
        with self._lock:
            self._fails.pop(key, None)
        return row["id"]

    # ---------- アカウント ----------
    @staticmethod
    def _check(name: str, password: str) -> str:
        name = str(name or "").strip()
        if not name or len(name) > 80:
            raise HTTPException(422, "ユーザー名は 1〜80 文字にしてください")
        if len(str(password or "")) < MIN_PASSWORD:
            raise HTTPException(422, f"パスワードは {MIN_PASSWORD} 文字以上にしてください")
        return name

    @staticmethod
    def _not_weak(name: str, password: str) -> None:
        """本人が決めるパスワードの条件。管理者が作る最初のパスワードには使わない。"""
        if len(str(password or "")) < MIN_PASSWORD:
            raise HTTPException(422, f"パスワードは {MIN_PASSWORD} 文字以上にしてください")
        if str(password).strip().casefold() == str(name).strip().casefold():
            raise HTTPException(422, "パスワードはユーザー名と違うものにしてください")

    def create_user(self, name: str, password: str, role: str = "member", uid: int | None = None) -> int:
        name = self._check(name, password)
        if role == "admin":
            self._not_weak(name, password)
        with self.db.write_lock:
            if self.db.one("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", (name,)):
                raise HTTPException(409, "同じユーザー名のアカウントがあります")
            cur = self.db.conn.execute("INSERT INTO users(id,name,pw_hash,role,created_at,must_change) VALUES(?,?,?,?,?,?)",
                                       (uid, name, hash_password(password), role, time.time(), int(role != "admin")))
            self.db.conn.commit()
        return cur.lastrowid

    def set_password(self, uid: int, password: str, keep_session: str | None = None) -> None:
        if len(str(password or "")) < MIN_PASSWORD:
            raise HTTPException(422, f"パスワードは {MIN_PASSWORD} 文字以上にしてください")
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET pw_hash=?,must_change=0 WHERE id=?", (hash_password(password), uid))
            self._drop_sessions(uid, keep_session)
            self.db.conn.commit()

    def reset_member(self, uid: int, password: str) -> None:
        """管理者による再設定。本人は次のログインで変える。認証アプリをなくした人もここで救う。"""
        self.set_password(uid, password)
        self._clear_second(uid)
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET must_change=1 WHERE id=?", (uid,))
            self.db.conn.commit()

    def change_password(self, user: dict, current: str, new: str, keep_session: str | None) -> None:
        self._confirm_self(user["id"], current)
        self._not_weak(user["name"], new)
        if str(new) == str(current):
            raise HTTPException(422, "今と違うパスワードにしてください")
        self.set_password(user["id"], new, keep_session=keep_session)

    def _drop_sessions(self, uid: int, keep_session: str | None) -> None:
        self.db.conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                             (uid, self._token_hash(keep_session) if keep_session else ""))

    def _confirm_self(self, uid: int, password: str) -> None:
        """ログイン中の人の本人確認。続けて間違えたら待たせる。"""
        key = ("#self", str(uid))
        wait = self._throttled(key)
        if wait:
            raise HTTPException(429, f"続けて間違えたので、{int(wait) + 1} 秒待ってください")
        if not check_password(str(password or ""), self.db.one("SELECT pw_hash FROM users WHERE id=?", (uid,))["pw_hash"]):
            self._failed(key)
            raise HTTPException(403, "今のパスワードが違います")
        with self._lock:
            self._fails.pop(key, None)

    # ---------- 二重認証 ----------
    def _clear_totp(self, uid: int) -> None:
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET totp_secret=NULL,totp_pending=NULL,totp_last=0 WHERE id=?", (uid,))
            self.db.conn.commit()

    def _clear_second(self, uid: int) -> None:
        self._clear_totp(uid)
        self.passkeys.clear(uid)

    def check_code(self, uid: int, code: str) -> None:
        row = self.db.one("SELECT totp_secret,totp_last FROM users WHERE id=?", (uid,))
        step = self._code_step(uid, row["totp_secret"], code, row["totp_last"])
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET totp_last=? WHERE id=?", (step, uid))
            self.db.conn.commit()

    def totp_start(self, user: dict, password: str) -> dict:
        self._confirm_self(user["id"], password)
        if self.db.one("SELECT totp_secret FROM users WHERE id=?", (user["id"],))["totp_secret"]:
            raise HTTPException(409, "二重認証はすでに設定されています")
        secret = base64.b32encode(os.urandom(20)).decode()
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET totp_pending=? WHERE id=?", (secret, user["id"]))
            self.db.conn.commit()
        uri = f"otpauth://totp/{TOTP_ISSUER}:{quote(user['name'])}?secret={secret}&issuer={TOTP_ISSUER}"
        return {"secret": secret, "uri": uri, "qr": segno.make(uri, error="m").svg_inline(scale=5, border=4)}

    def totp_confirm(self, user: dict, code: str, keep_session: str | None) -> None:
        row = self.db.one("SELECT totp_pending FROM users WHERE id=?", (user["id"],))
        if not row["totp_pending"]:
            raise HTTPException(409, "先に二重認証の設定を始めてください")
        step = self._code_step(user["id"], row["totp_pending"], code, 0)
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET totp_secret=totp_pending,totp_pending=NULL,totp_last=? WHERE id=?", (step, user["id"]))
            self._drop_sessions(user["id"], keep_session)    # 二重認証の前からログインしていた端末は入り直す
            self.db.conn.commit()

    def totp_off(self, user: dict, password: str, code: str) -> None:
        self._confirm_self(user["id"], password)
        row = self.db.one("SELECT totp_secret,totp_last FROM users WHERE id=?", (user["id"],))
        if not row["totp_secret"]:
            raise HTTPException(409, "二重認証は設定されていません")
        self._code_step(user["id"], row["totp_secret"], code, row["totp_last"])
        self._clear_totp(user["id"])

    def _code_step(self, uid: int, secret: str, code: str, after: int) -> int:
        key = ("#code", str(uid))
        wait = self._throttled(key)
        if wait:
            raise HTTPException(429, f"続けて間違えたので、{int(wait) + 1} 秒待ってください")
        step = totp_match(secret, code, after)
        if step is None:
            self._failed(key)
            raise HTTPException(403, "コードが違います。認証アプリの今の数字を入れてください")
        with self._lock:
            self._fails.pop(key, None)
        return step

    def delete_user(self, uid: int) -> None:
        with self.db.write_lock:
            c = self.db.conn
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("DELETE FROM passkeys WHERE user_id=?", (uid,))
            c.execute("DELETE FROM track_stats WHERE user_id=?", (uid,))
            c.execute("DELETE FROM user_settings WHERE user_id=?", (uid,))
            c.execute("DELETE FROM lyrics_personal WHERE user_id=?", (uid,))
            c.execute("DELETE FROM playlists WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))
            c.commit()

    # ---------- ログイン ----------
    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _throttled(self, key: tuple[str, str], limit: int = MAX_FAILS) -> float:
        with self._lock:
            n, until = self._fails.get(key, (0, 0.0))
        return max(0.0, until - time.time()) if n >= limit else 0.0

    def _failed(self, key: tuple[str, str], limit: int = MAX_FAILS) -> None:
        with self._lock:
            n = self._fails.get(key, (0, 0.0))[0] + 1
            wait = min(900, 30 * 2 ** max(0, n - limit)) if n >= limit else 0
            self._fails[key] = (n, time.time() + wait)
            if len(self._fails) > 5000:      # 大量の名前を試されても記憶を増やし続けない
                now = time.time()
                self._fails = {k: v for k, v in self._fails.items() if v[1] > now}

    def login(self, request: Request, response: Response, name: str, password: str, code: str = "") -> dict:
        name = str(name or "").strip()
        ip = request.client.host if request.client else ""
        key, by_name, by_ip = (name.casefold(), ip), (name.casefold(), "*"), ("*", ip)
        wait = max(self._throttled(key), self._throttled(by_name, MAX_NAME_FAILS), self._throttled(by_ip, MAX_IP_FAILS))
        if wait:
            raise HTTPException(429, f"続けて間違えたので、{int(wait) + 1} 秒待ってください")
        if not self._hashing.acquire(blocking=False):
            raise HTTPException(429, "混み合っています。少し待ってからもう一度試してください")
        try:
            row = self.db.one("SELECT * FROM users WHERE name=? COLLATE NOCASE", (name,))
            ok = check_password(str(password or ""), row["pw_hash"] if row else _DUMMY)
        finally:
            self._hashing.release()
        if not row or not ok:
            self._failed(key)
            self._failed(by_name, MAX_NAME_FAILS)
            self._failed(by_ip, MAX_IP_FAILS)
            raise HTTPException(401, "ユーザー名かパスワードが違います")
        if row["totp_secret"]:
            if not str(code or "").strip():
                raise HTTPException(401, "認証アプリの 6 桁のコードを入れてください", headers={"X-Need-Code": "1"})
            step = totp_match(row["totp_secret"], code, row["totp_last"])
            if step is None:
                self._failed(key)
                self._failed(by_name, MAX_NAME_FAILS)
                self._failed(by_ip, MAX_IP_FAILS)
                raise HTTPException(401, "コードが違います。認証アプリの今の数字を入れてください", headers={"X-Need-Code": "1"})
            with self.db.write_lock:
                self.db.conn.execute("UPDATE users SET totp_last=? WHERE id=?", (step, row["id"]))
                self.db.conn.commit()
        elif row["role"] == "admin" and self.passkeys.count(row["id"]):
            raise HTTPException(401, "管理者は指紋でログインしてください", headers={"X-Need-Passkey": "1"})
        with self._lock:
            for k in (key, by_name, by_ip):
                self._fails.pop(k, None)
        self.start_session(request, response, row["id"])
        return self.public(row)

    def start_session(self, request: Request, response: Response, uid: int) -> None:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.write_lock:
            self.db.conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
            self.db.conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                                 (self._token_hash(token), uid, now, now + SESSION_S))
            self.db.conn.commit()
        response.set_cookie(COOKIE, token, max_age=SESSION_S, httponly=True, samesite="lax", path="/",
                            secure=request.url.scheme == "https")

    def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(COOKIE)
        if token:
            with self.db.write_lock:
                self.db.conn.execute("DELETE FROM sessions WHERE token_hash=?", (self._token_hash(token),))
                self.db.conn.commit()
        response.delete_cookie(COOKIE, path="/")

    def user_of(self, request: Request) -> dict | None:
        if not self.enabled:
            return {"id": 1, "name": "admin", "role": "admin"}
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        row = self.db.one("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                          (self._token_hash(token), time.time()))
        return self.public(row) if row else None

    def public(self, row) -> dict:
        keys = self.passkeys.count(row["id"])
        return {"id": row["id"], "name": row["name"], "role": row["role"], "mustChange": bool(row["must_change"]), "totp": bool(row["totp_secret"]),
                "passkeys": keys, "needsSecond": row["role"] == "admin" and not row["totp_secret"] and not keys}

    # ---------- Cast・DLNA の機器に渡す署名 ----------
    def _secret(self) -> bytes:
        s = self.db.meta("link_secret")
        if not s:
            s = self.db.meta("link_secret", secrets.token_hex(32))
        return s.encode()

    def link(self, kind: str, oid: int, ttl: int = LINK_S) -> str:
        exp = int(time.time()) + ttl
        return f"{exp}.{self._sign(kind, oid, exp)}"

    def _sign(self, kind: str, oid: int, exp: int) -> str:
        return hmac.new(self._secret(), f"{kind}:{oid}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]

    def link_ok(self, request: Request) -> bool:
        token = request.query_params.get("t")
        if not token or request.method not in ("GET", "HEAD"):
            return False
        try:
            exp_s, sig = token.split(".", 1)
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < time.time():
            return False
        for kind, pat in _LINKABLE.items():
            m = pat.fullmatch(request.url.path)
            if m:
                return hmac.compare_digest(sig, self._sign(kind, int(m.group(1)), exp))
        return False

    # ---------- すべての API の入口 ----------
    def guard(self, request: Request) -> None:
        path = request.url.path
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            return
        if self.link_ok(request):
            request.state.user = None
            return
        user = self.user_of(request)
        if user is None:
            raise HTTPException(401, "ログインしてください")
        request.state.user = user
        if user.get("mustChange"):
            raise HTTPException(403, "先にパスワードを変更してください")
        if user.get("needsSecond"):
            raise HTTPException(403, "先に指紋を登録してください")
        if user["role"] == "admin":
            return
        if path.startswith("/api/dlna/") and not is_home(request):
            raise HTTPException(403, "DLNA は実家のアドレスから開いたときに使えます")
        if request.method in ("GET", "HEAD"):
            allowed = not any(p.fullmatch(path) for p in ADMIN_READS)
        else:
            allowed = any(p.fullmatch(path) for p in MEMBER_WRITES)
        if not allowed:
            raise HTTPException(403, "この操作は管理者だけができます")


def main(argv: list[str]) -> int:
    if argv != ["reset-code"]:
        print("使い方: python -m otokura.auth reset-code")
        return 2
    code = write_reset_code(Path(os.environ.get("OTOKURA_DATA", "/data")))
    print(f"確認コード: {code}")
    print("音蔵のログイン画面の「パスワードを忘れた」に入力してください。15 分で使えなくなります。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
