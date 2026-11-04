"""アーティスト画像・紹介文とアルバム情報を Web から取ってきて data に残す。音楽ファイルには書き込まない。

- 調べる先: MusicBrainz(誰・どの盤か、発売日、レーベル)、Wikidata・Wikipedia(紹介文、写真)、
  Deezer(アーティスト写真)、iTunes(MusicBrainz に無いアルバムの発売日)
- 同名の別人を避けるため、名前が完全に一致し、手元のアルバムと 1 枚でも重なる候補を選ぶ。
  確かめられないものは「見つからない」のままにする(候補が 1 人だけの MusicBrainz は名前の一致で採る)
- 一度に 1 件ずつ。MusicBrainz は 1 秒 1 回までの決まりなので間を空ける
- 更新の確認: 取ってから日数が経ったものを調べ直し、内容が変わっていたら updated_at を更新する。
  通信の失敗では今ある情報を消さない
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from urllib.parse import quote

import httpx

from . import __version__, art

log = logging.getLogger("otokura.webinfo")

USER_AGENT = f"Otokura/{__version__} ( https://github.com/crosssync-collab/otokura )"
MB = "https://musicbrainz.org/ws/2"
INTERVAL_S = {"musicbrainz.org": 1.1}
DEFAULT_INTERVAL_S = 0.25
STALE_DAYS = 30
DAILY_LIMIT = 300
MAX_IMAGE_BYTES = 10 * 1024 ** 2
SKIP_ARTISTS = {"various artists", "不明なアーティスト", "unknown artist"}


class Transient(Exception):
    """通信の失敗。今ある情報はそのままにして、あとでやり直す。"""


def norm(s: str | None) -> str:
    """表記の違いを無視して比べるための形(全角半角・大小・記号・空白)。"""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(c for c in s if c.isalnum())


def _lucene(s: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", s) + '"'


def _album_titles(titles) -> set[str]:
    out = set()
    for t in titles:
        n = norm(re.sub(r"[\(\[(【].*?[\)\])】]", "", t or "")) or norm(t)
        if n:
            out.add(n)
    return out


class WebInfo:
    def __init__(self, db, art_dir, client: httpx.Client | None = None):
        self.db, self.art_dir = db, art_dir
        self.client = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self._last: dict[str, float] = {}
        self._queue: deque[tuple[str, str]] = deque()
        self._queued: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.state = {"running": False, "done": 0, "total": 0, "errors": 0, "updated": 0}

    # ---------- 通信 ----------
    def _get(self, url: str, params: dict | None = None, retry: bool = True) -> httpx.Response | None:
        """404 は None。429・5xx と接続の失敗は Transient。"""
        host = httpx.URL(url).host
        gap = INTERVAL_S.get(host, DEFAULT_INTERVAL_S) - (time.monotonic() - self._last.get(host, 0))
        if gap > 0:
            time.sleep(gap)
        try:
            r = self.client.get(url, params=params)
        except httpx.HTTPError as e:
            raise Transient(f"{host}: {e}") from e
        finally:
            self._last[host] = time.monotonic()
        if r.status_code == 404:
            return None
        if r.status_code in (429, 503) and retry:
            try:
                wait = min(30.0, float(r.headers.get("retry-after") or 5))
            except ValueError:
                wait = 5.0
            time.sleep(wait)
            return self._get(url, params, retry=False)
        if r.status_code >= 400:
            raise Transient(f"{host}: HTTP {r.status_code}")
        return r

    def _json(self, url: str, params: dict | None = None) -> dict | None:
        r = self._get(url, params)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise Transient(f"{url}: {e}") from e

    # ---------- MusicBrainz・Wikidata・Wikipedia ----------
    def _mb_artist(self, name: str, albums: set[str]) -> str | None:
        j = self._json(f"{MB}/artist", {"query": f"artist:{_lucene(name)}", "fmt": "json", "limit": 8}) or {}
        n = norm(name)
        cands = [a for a in j.get("artists") or []
                 if n and (norm(a.get("name")) == n or any(norm(x.get("name")) == n for x in a.get("aliases") or []))]
        if not cands:
            return None
        best, score = None, 0
        for a in cands[:3]:
            rg = self._json(f"{MB}/release-group", {"artist": a["id"], "limit": 100, "fmt": "json"}) or {}
            hit = len(albums & _album_titles(x.get("title") for x in rg.get("release-groups") or []))
            if hit > score:
                best, score = a["id"], hit
        return best or (cands[0]["id"] if len(cands) == 1 else None)

    def _wikidata_id(self, entity: str, mbid: str) -> str | None:
        j = self._json(f"{MB}/{entity}/{mbid}", {"inc": "url-rels", "fmt": "json"}) or {}
        for rel in j.get("relations") or []:
            m = re.search(r"wikidata\.org/wiki/(Q\d+)", (rel.get("url") or {}).get("resource") or "")
            if m:
                return m.group(1)
        return None

    def _wikidata(self, qid: str) -> tuple[str | None, dict]:
        """(写真のファイル名, {'jawiki': 記事名, ...})"""
        j = self._json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json") or {}
        ent = (j.get("entities") or {}).get(qid) or {}
        image = None
        for c in (ent.get("claims") or {}).get("P18") or []:
            image = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if image:
                break
        return image, {k: (v or {}).get("title") for k, v in (ent.get("sitelinks") or {}).items()}

    def _summary(self, sites: dict) -> tuple[str | None, str | None]:
        for site, host in (("jawiki", "ja.wikipedia.org"), ("enwiki", "en.wikipedia.org")):
            title = sites.get(site)
            if not title:
                continue
            j = self._json(f"https://{host}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}")
            if j and j.get("type") != "disambiguation" and j.get("extract"):
                return j["extract"], ((j.get("content_urls") or {}).get("desktop") or {}).get("page")
        return None, None

    # ---------- アーティスト ----------
    def _deezer_picture(self, name: str, albums: set[str]) -> str | None:
        j = self._json("https://api.deezer.com/search/artist", {"q": name, "limit": 10}) or {}
        n = norm(name)
        for a in (j.get("data") or [])[:10]:
            if norm(a.get("name")) != n:
                continue
            pic = a.get("picture_xl") or ""
            if not pic or "/artist//" in pic:      # 写真が登録されていない
                continue
            al = self._json(f"https://api.deezer.com/artist/{a['id']}/albums", {"limit": 100}) or {}
            if albums & _album_titles(x.get("title") for x in al.get("data") or []):
                return pic
        return None

    def _image(self, url: str, key: str) -> str | None:
        r = self._get(url)
        if r is None or len(r.content) > MAX_IMAGE_BYTES:
            return None
        res = art.make_thumbs(r.content, self.art_dir, key)
        return res[0] if res else None

    def fetch_artist(self, name: str) -> dict:
        out = {"mbid": None, "image": None, "image_source": None, "summary": None, "summary_url": None}
        if norm(name) in {norm(x) for x in SKIP_ARTISTS}:
            return out
        rows = self.db.q("SELECT title FROM albums WHERE artist=?", (name,))
        albums = _album_titles(r["title"] for r in rows)
        mbid = out["mbid"] = self._mb_artist(name, albums)
        commons, sites = None, {}
        if mbid:
            qid = self._wikidata_id("artist", mbid)
            if qid:
                commons, sites = self._wikidata(qid)
                out["summary"], out["summary_url"] = self._summary(sites)
        key = f"artist|{name}"
        pic = self._deezer_picture(name, albums)
        if pic:
            out["image"], out["image_source"] = self._image(pic, key), "Deezer"
        if not out["image"] and commons:
            url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(commons.replace(' ', '_'), safe='')}?width=1000"
            out["image"], out["image_source"] = self._image(url, key), "Wikimedia Commons"
        if not out["image"]:
            out["image_source"] = None
        return out

    # ---------- アルバム ----------
    def fetch_album(self, title: str, artist: str) -> dict:
        out = {"mbid": None, "release_date": None, "kind": None, "label": None, "summary": None, "summary_url": None, "source": None}
        q = f"releasegroup:{_lucene(title)}"
        va = norm(artist) in {norm(x) for x in SKIP_ARTISTS}
        if not va:
            q += f" AND artist:{_lucene(artist)}"
        j = self._json(f"{MB}/release-group", {"query": q, "fmt": "json", "limit": 8}) or {}
        hit = None
        for rg in j.get("release-groups") or []:
            if norm(rg.get("title")) != norm(title):
                continue
            names = {norm(c.get("name")) for c in rg.get("artist-credit") or []} | \
                    {norm((c.get("artist") or {}).get("name")) for c in rg.get("artist-credit") or []}
            if va or norm(artist) in names:
                hit = rg
                break
        if hit:
            out.update(mbid=hit["id"], release_date=hit.get("first-release-date") or None,
                       kind=hit.get("primary-type"), source="MusicBrainz")
            rel = self._json(f"{MB}/release", {"release-group": hit["id"], "inc": "labels", "limit": 1, "fmt": "json"}) or {}
            for r in rel.get("releases") or []:
                for li in r.get("label-info") or []:
                    name = (li.get("label") or {}).get("name")
                    if name and not out["label"]:
                        out["label"] = name
            qid = self._wikidata_id("release-group", hit["id"])
            if qid:
                out["summary"], out["summary_url"] = self._summary(self._wikidata(qid)[1])
        if not out["release_date"] and not va:
            j = self._json("https://itunes.apple.com/search",
                           {"term": f"{artist} {title}", "entity": "album", "country": "jp", "limit": 10}) or {}
            for a in j.get("results") or []:
                if norm(a.get("collectionName")) == norm(title) and norm(a.get("artistName")) == norm(artist):
                    out["release_date"] = (a.get("releaseDate") or "")[:10] or None
                    out["source"] = out["source"] or "iTunes"
                    break
        return out

    # ---------- 保存 ----------
    def _save(self, table: str, key_col: str, key: str, data: dict) -> bool:
        """保存する。前回から内容が変わっていれば True。"""
        found = any(data.get(k) for k in ("image", "summary", "release_date", "label"))
        digest = hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        now = time.time()
        old = self.db.one(f"SELECT digest FROM {table} WHERE {key_col}=?", (key,))
        changed = bool(old and old["digest"] != digest)
        cols = list(data)
        with self.db.write_lock:
            self.db.conn.execute(
                f"INSERT INTO {table}({key_col},{','.join(cols)},status,digest,fetched_at,first_at,updated_at) "
                f"VALUES({','.join('?' * (len(cols) + 6))}) ON CONFLICT({key_col}) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols)
                + ",status=excluded.status,digest=excluded.digest,fetched_at=excluded.fetched_at"
                + (",updated_at=excluded.updated_at" if changed else ""),
                (key, *[data[c] for c in cols], "ok" if found else "none", digest, now, now, now))
            self.db.conn.commit()
        return changed

    def update_artist(self, name: str) -> bool:
        return self._save("artist_info", "name", name, self.fetch_artist(name))

    def update_album(self, key: str) -> bool:
        r = self.db.one("SELECT title, artist FROM albums WHERE key=?", (key,))
        if r is None:
            return False
        return self._save("album_info", "key", key, self.fetch_album(r["title"], r["artist"]))

    # ---------- 順番待ち ----------
    def enqueue(self, kind: str, key: str, first: bool = False) -> None:
        item = (kind, key)
        with self._lock:
            if item in self._queued:
                if first:
                    self._queue.remove(item)
                    self._queue.appendleft(item)
            else:
                self._queued.add(item)
                self._queue.appendleft(item) if first else self._queue.append(item)
                self.state["total"] += 1
            if self._worker is None or not self._worker.is_alive():
                self.state.update(running=True)
                self._worker = threading.Thread(target=self._run, daemon=True, name="webinfo")
                self._worker.start()

    def enqueue_missing(self) -> int:
        """まだ調べていないアーティストとアルバムをすべて並べる。"""
        rows = self.db.q("SELECT DISTINCT a.artist FROM albums a LEFT JOIN artist_info i ON i.name=a.artist WHERE i.name IS NULL")
        als = self.db.q("SELECT a.key FROM albums a LEFT JOIN album_info i ON i.key=a.key WHERE i.key IS NULL")
        for r in rows:
            self.enqueue("artist", r["artist"])
        for r in als:
            self.enqueue("album", r["key"])
        return len(rows) + len(als)

    def enqueue_stale(self, days: float = STALE_DAYS, limit: int | None = None) -> int:
        """調べてから日数が経ったものを古い順に並べる(更新の確認)。"""
        cut = time.time() - days * 86400
        rows = [("artist", r["name"], r["fetched_at"]) for r in self.db.q(
            "SELECT i.name, i.fetched_at FROM artist_info i JOIN albums a ON a.artist=i.name WHERE i.fetched_at<? GROUP BY i.name", (cut,))]
        rows += [("album", r["key"], r["fetched_at"]) for r in self.db.q(
            "SELECT i.key, i.fetched_at FROM album_info i JOIN albums a ON a.key=i.key WHERE i.fetched_at<?", (cut,))]
        rows.sort(key=lambda x: x[2])
        rows = rows[:limit] if limit else rows
        for kind, key, _ in rows:
            self.enqueue(kind, key)
        return len(rows)

    def daily(self) -> int:
        """スキャンのあとに呼ぶ。1 日 1 回、古いものを少しずつ確かめる。"""
        today = time.strftime("%Y-%m-%d")
        if self.db.meta("webinfo_daily") == today:
            return 0
        self.db.meta("webinfo_daily", today)
        return self.enqueue_stale(limit=DAILY_LIMIT)

    def stop(self) -> None:
        with self._lock:
            self.state["total"] -= len(self._queue)
            self._queue.clear()
            self._queued.clear()

    def wait(self, timeout: float = 10) -> None:
        """終わるまで待つ(テスト用)。"""
        w = self._worker
        if w:
            w.join(timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self.state.update(running=False, done=0, total=0)
                    return
                item = self._queue.popleft()
            kind, key = item
            try:
                changed = self.update_artist(key) if kind == "artist" else self.update_album(key)
                if changed:
                    self.state["updated"] += 1
            except Transient as e:
                self.state["errors"] += 1
                log.warning("情報を取れませんでした: %s %s: %s", kind, key, e)
            except Exception:
                self.state["errors"] += 1
                log.exception("情報を取れませんでした: %s %s", kind, key)
            with self._lock:
                self._queued.discard(item)
                self.state["done"] += 1

    def public(self) -> dict:
        def counts(table: str, total_sql: str) -> dict:
            rows = {r["status"]: r["n"] for r in self.db.q(f"SELECT status, COUNT(*) n FROM {table} GROUP BY status")}
            return {"ok": rows.get("ok", 0), "none": rows.get("none", 0), "total": self.db.one(total_sql)["n"]}
        # 2 回目以降の確認で内容が変わったもの(初回は digest が無いので updated_at = first_at のまま)
        cut = time.time() - 30 * 86400
        recent = [{"kind": "artist", "name": r["name"], "at": r["updated_at"]} for r in self.db.q(
            "SELECT name, updated_at FROM artist_info WHERE updated_at>first_at AND updated_at>? ORDER BY updated_at DESC LIMIT 10", (cut,))]
        recent += [{"kind": "album", "name": r["title"], "at": r["updated_at"]} for r in self.db.q(
            "SELECT a.title, i.updated_at FROM album_info i JOIN albums a ON a.key=i.key WHERE i.updated_at>i.first_at AND i.updated_at>? "
            "ORDER BY i.updated_at DESC LIMIT 10", (cut,))]
        recent.sort(key=lambda x: -x["at"])
        return {**self.state, "queued": len(self._queue),
                "artists": counts("artist_info", "SELECT COUNT(DISTINCT artist) n FROM albums"),
                "albums": counts("album_info", "SELECT COUNT(*) n FROM albums"), "recent": recent[:10]}
