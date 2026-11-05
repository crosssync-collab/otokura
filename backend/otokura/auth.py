"""音蔵のアカウント。家族に NAS の権限を渡さず、音蔵の中だけで人を分ける。

- 管理者(admin)と家族(member)の 2 種類。アカウントは音蔵の DB だけに置く(NAS のユーザーは増やさない)
- パスワードは scrypt。ログインは 1 年間そのまま(Cookie の値は DB にハッシュで置く)。続けて間違えると待ち時間を入れる
- 家族の書き込みは MEMBER_WRITES の許可リストだけ。新しい API は既定で管理者だけになる
- Cast・DLNA の機器はログインできないので、曲ごと・期限付きの署名を URL に付ける
- 管理者がパスワードを忘れたら、NAS で `docker exec otokura python -m otokura.auth reset-code` を実行して確認コードを作り、
  ログイン画面で再設定する。NAS に入れる人だけができる。家族のパスワードは管理者が画面で再設定する
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
import sys
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request, Response

log = logging.getLogger("otokura.auth")

COOKIE = "otokura_s"
SESSION_S = 365 * 86400
LINK_S = 24 * 3600
MIN_PASSWORD = 8
MAX_FAILS = 5
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
RESET_S = 15 * 60
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


def is_home(request: Request) -> bool:
    """家の中のネットワークからの接続か。Tailscale・転送元が不明な接続は外として扱う。"""
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
        self.set_password(row["id"], password)    # 短いパスワードならここで止まり、コードは使い切らない
        path.unlink(missing_ok=True)
        with self._lock:
            self._fails.pop(key, None)
        return row["id"]

    # ---------- アカウント ----------
    @staticmethod
    def _check(name: str, password: str) -> str:
        name = str(name or "").strip()
        if not name or len(name) > 40:
            raise HTTPException(422, "名前は 1〜40 文字にしてください")
        if len(str(password or "")) < MIN_PASSWORD:
            raise HTTPException(422, f"パスワードは {MIN_PASSWORD} 文字以上にしてください")
        return name

    def create_user(self, name: str, password: str, role: str = "member", uid: int | None = None) -> int:
        name = self._check(name, password)
        with self.db.write_lock:
            if self.db.one("SELECT 1 FROM users WHERE name=?", (name,)):
                raise HTTPException(409, "同じ名前のアカウントがあります")
            cur = self.db.conn.execute("INSERT INTO users(id,name,pw_hash,role,created_at) VALUES(?,?,?,?,?)",
                                       (uid, name, hash_password(password), role, time.time()))
            self.db.conn.commit()
        return cur.lastrowid

    def set_password(self, uid: int, password: str, keep_session: str | None = None) -> None:
        if len(str(password or "")) < MIN_PASSWORD:
            raise HTTPException(422, f"パスワードは {MIN_PASSWORD} 文字以上にしてください")
        with self.db.write_lock:
            self.db.conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), uid))
            self.db.conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                                 (uid, self._token_hash(keep_session) if keep_session else ""))
            self.db.conn.commit()

    def delete_user(self, uid: int) -> None:
        with self.db.write_lock:
            c = self.db.conn
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
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

    def _throttled(self, key: tuple[str, str]) -> float:
        with self._lock:
            n, until = self._fails.get(key, (0, 0.0))
        return max(0.0, until - time.time()) if n >= MAX_FAILS else 0.0

    def _failed(self, key: tuple[str, str]) -> None:
        with self._lock:
            n = self._fails.get(key, (0, 0.0))[0] + 1
            wait = min(900, 30 * 2 ** max(0, n - MAX_FAILS)) if n >= MAX_FAILS else 0
            self._fails[key] = (n, time.time() + wait)

    def login(self, request: Request, response: Response, name: str, password: str) -> dict:
        name = str(name or "").strip()
        key = (name.casefold(), request.client.host if request.client else "")
        wait = self._throttled(key)
        if wait:
            raise HTTPException(429, f"続けて間違えたので、{int(wait) + 1} 秒待ってください")
        row = self.db.one("SELECT * FROM users WHERE name=?", (name,))
        ok = check_password(str(password or ""), row["pw_hash"] if row else _DUMMY)
        if not row or not ok:
            self._failed(key)
            raise HTTPException(401, "名前かパスワードが違います")
        with self._lock:
            self._fails.pop(key, None)
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

    @staticmethod
    def public(row) -> dict:
        return {"id": row["id"], "name": row["name"], "role": row["role"]}

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
        if user["role"] == "admin":
            return
        if path.startswith("/api/dlna/") and not is_home(request):
            raise HTTPException(403, "DLNA は家の中のアドレスから開いたときに使えます")
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
