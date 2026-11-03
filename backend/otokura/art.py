"""カバー画像: 縮小版を /data/art に作り、画面の配色に使う色相を求める。"""
from __future__ import annotations

import colorsys
import hashlib
import io
from pathlib import Path

SIZES = (300, 1000)
FOLDER_NAMES = ("cover", "folder", "front", "albumart", "album", "jacket")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def find_folder_image(folder: Path) -> Path | None:
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXT]
    except OSError:
        return None
    by_stem = {p.stem.lower(): p for p in files}
    for n in FOLDER_NAMES:
        if n in by_stem:
            return by_stem[n]
    return files[0] if len(files) == 1 else None   # 画像が 1 枚だけならそれを使う


def dominant_hue(img) -> int | None:
    """彩度の高い色を重視して色相を求める。ほぼ無彩色なら None。"""
    small = img.convert("RGB").resize((32, 32))
    hx = hy = wsum = 0.0
    import math
    raw = small.tobytes()
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        w = s * (1 - abs(l - 0.5) * 2)       # 鮮やかで中間の明るさほど重い
        if w <= 0.05:
            continue
        hx += math.cos(h * 2 * math.pi) * w
        hy += math.sin(h * 2 * math.pi) * w
        wsum += w
    if wsum < 20:
        return None
    return int((math.degrees(math.atan2(hy, hx)) + 360) % 360)


def make_thumbs(data: bytes, art_dir: Path, key: str) -> tuple[str, int | None] | None:
    """画像データから縮小版を作る。戻り値は (ファイル名の元, 色相)。読めない画像なら None。"""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return None
    # 画像の中身も名前に入れる(カバーを差し替えたら名前が変わり、ブラウザの古いキャッシュを使わない)
    name = hashlib.sha1(key.encode("utf-8") + hashlib.sha1(data).digest()).hexdigest()[:20]
    art_dir.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        t = img.copy()
        t.thumbnail((size, size), Image.LANCZOS)
        t.save(art_dir / f"{name}_{size}.jpg", "JPEG", quality=86, optimize=True, progressive=True)
    return name, dominant_hue(img)


def thumb_path(art_dir: Path, name: str, size: int) -> Path:
    size = min(SIZES, key=lambda s: abs(s - size) if s >= size else 10_000 + size - s)
    return art_dir / f"{name}_{size}.jpg"
