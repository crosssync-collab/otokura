"""LRCLIB クライアント(歌詞が手元に無い曲だけに使う)。

エンドポイント: GET /api/get(track_name・artist_name 必須、album_name・duration 任意。
見つからなければ 404)と GET /api/search。応答は trackName / artistName / albumName / duration /
instrumental / plainLyrics / syncedLyrics。長さは約 ±2 秒で照合される。
公式のレート制限値は未確認のため、1 秒 1 回に抑え、429 は Retry-After を尊重する。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .lrc import parse_lrc, parse_plain
from .model import Layer, Lyrics

BASE = "https://lrclib.net"
USER_AGENT = "Otokura/0.1 (self-hosted music player; https://github.com/)"
DURATION_TOLERANCE_S = 2.0


@dataclass
class Query:
    title: str
    artist: str
    album: str | None = None
    duration_ms: int | None = None


def _to_lyrics(d: dict, track_ms: int | None) -> Lyrics | None:
    if d.get("instrumental"):
        return Lyrics(source="lrclib", instrumental=True, layers=[Layer(kind="main")])
    if d.get("syncedLyrics"):
        ly = parse_lrc(d["syncedLyrics"], source="lrclib", track_ms=track_ms)
    elif d.get("plainLyrics"):
        ly = parse_plain(d["plainLyrics"], source="lrclib")
    else:
        return None
    if d.get("lang") and ly.main and not ly.main.lang:
        ly.main.lang = d["lang"]
    return ly


def _close(d: dict, q: Query) -> bool:
    if not q.duration_ms or not d.get("duration"):
        return True
    return abs(float(d["duration"]) - q.duration_ms / 1000) <= DURATION_TOLERANCE_S


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"LRCLIB の利用制限に達しました。{retry_after:.0f} 秒後に再試行してください")
        self.retry_after = retry_after


class LrcLib:
    """min_interval_s: 連続して問い合わせるときの最短間隔(夜間の一括取得で LRCLIB に負荷をかけないため)。
    公式のレート制限値は未確認なので、控えめに 1 秒 1 回にしている。"""

    def __init__(self, client: httpx.Client | None = None, base: str = BASE, min_interval_s: float = 1.0):
        self.base = base.rstrip("/")
        self.client = client or httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT})
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def _get(self, path: str, params: dict) -> httpx.Response:
        import time
        wait = self._last + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.client.get(f"{self.base}{path}", params=params)
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("Retry-After", "60"))
            except ValueError:
                ra = 60.0
            raise RateLimited(ra)
        return r

    def fetch(self, q: Query) -> Lyrics | None:
        """完全一致(/api/get)→ 見つからなければ検索(/api/search)。長さが2秒以上違う候補は捨てる。"""
        params = {"track_name": q.title, "artist_name": q.artist}
        if q.album:
            params["album_name"] = q.album
        if q.duration_ms:
            params["duration"] = round(q.duration_ms / 1000)
        r = self._get("/api/get", params)
        if r.status_code == 200:
            d = r.json()
            if _close(d, q):
                ly = _to_lyrics(d, q.duration_ms)
                if ly:
                    return ly
        elif r.status_code not in (404,):
            r.raise_for_status()

        r = self._get("/api/search", {"track_name": q.title, "artist_name": q.artist})
        if r.status_code != 200:
            return None
        cands = [d for d in r.json() if _close(d, q)]
        # 同期歌詞があるものを優先し、次に長さが近いもの
        cands.sort(key=lambda d: (not d.get("syncedLyrics"),
                                  abs(float(d.get("duration") or 0) - (q.duration_ms or 0) / 1000)))
        for d in cands:
            ly = _to_lyrics(d, q.duration_ms)
            if ly:
                return ly
        return None
