"""環境変数から設定を読む。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Source:
    id: str        # URL や DB で使う短い名前(英数字)
    name: str      # 画面に出す名前
    path: Path


@dataclass
class Config:
    sources: list[Source] = field(default_factory=list)
    data_dir: Path = Path("/data")
    scan_at: str | None = "03:00"       # 毎日の差分スキャン時刻(None で無効)
    scan_on_start: bool = True
    lrclib: bool = True
    web_dir: Path | None = None
    lyrics_dirs: list[Path] = field(default_factory=list)   # 「アーティスト - 曲名.lrc」が並ぶ歌詞フォルダ
    cast_url: str | None = None   # Google Cast の機器が曲を取りに来るアドレス(家の中の http://NAS:8686)
    transcode_max_gb: float = 20.0   # 変換したファイルを残しておく上限(data/transcode)
    webinfo: bool = True     # アーティスト画像・アルバム情報を Web から取る

    @property
    def db_path(self) -> Path:
        return self.data_dir / "otokura.db"

    @property
    def art_dir(self) -> Path:
        return self.data_dir / "art"


def _slug(name: str, used: set[str]) -> str:
    base = "".join(c for c in name.lower() if c.isalnum()) or "src"
    s, n = base, 2
    while s in used:
        s, n = f"{base}{n}", n + 1
    used.add(s)
    return s


def parse_sources(value: str) -> list[Source]:
    """'Lossless=/music/Lossless,1.Lossy=/music/1.Lossy' → [Source, ...]"""
    out: list[Source] = []
    used: set[str] = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        name, _, path = part.partition("=")
        if not path:
            name, path = Path(part).name, part
        out.append(Source(id=_slug(name, used), name=name.strip(), path=Path(path.strip())))
    return out


def from_env(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    web = e.get("OTOKURA_WEB_DIR")
    if not web:
        guess = Path(__file__).resolve().parents[2] / "web"
        web = str(guess) if guess.exists() else ""
    return Config(
        sources=parse_sources(e.get("OTOKURA_SOURCES", "Lossless=/music/Lossless,1.Lossy=/music/1.Lossy")),
        data_dir=Path(e.get("OTOKURA_DATA", "/data")),
        scan_at=(e.get("OTOKURA_SCAN_AT", "03:00") or None),
        scan_on_start=e.get("OTOKURA_SCAN_ON_START", "1") not in ("0", "false", "no"),
        lrclib=e.get("OTOKURA_LRCLIB", "1") not in ("0", "false", "no"),
        web_dir=Path(web) if web else None,
        lyrics_dirs=[Path(p.strip()) for p in e.get("OTOKURA_LYRICS", "").split(",") if p.strip()],
        cast_url=(e.get("OTOKURA_CAST_URL") or "").rstrip("/") or None,
        transcode_max_gb=float(e.get("OTOKURA_TRANSCODE_MAX_GB") or 20),
        webinfo=e.get("OTOKURA_WEBINFO", "1") not in ("0", "false", "no"),
    )
