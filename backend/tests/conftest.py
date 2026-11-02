import shutil
import subprocess
from pathlib import Path

import pytest

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def make_audio(path: Path, seconds=3, codec_args=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", str(seconds), *codec_args, str(path)], check=True)
    return path


def png_bytes(color=(200, 40, 60)):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 400), color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def library(tmp_path):
    """2 ソース分の小さなライブラリを作る。"""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg が必要")
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TRCK, TDRC, Encoding

    lossless = tmp_path / "music" / "Lossless"
    lossy = tmp_path / "music" / "1.Lossy"

    # 1) タグ付き FLAC 2 曲 + 埋め込みカバー
    for n, title in ((1, "夜行バス"), (2, "窓の外の街")):
        p = make_audio(lossless / "深谷 あおい" / "夜行バス (2023)" / f"0{n} {title}.flac")
        f = FLAC(p)
        f.update({"TITLE": title, "ARTIST": "深谷 あおい", "ALBUM": "夜行バス", "DATE": "2023",
                  "TRACKNUMBER": str(n), "GENRE": "アンビエント"})
        if n == 1:
            pic = Picture(); pic.type = 3; pic.mime = "image/png"; pic.data = png_bytes()
            f.add_picture(pic)
        f.save()
    (lossless / "深谷 あおい" / "夜行バス (2023)" / "01 夜行バス.lrc").write_text("[00:00.50]窓に映る\n[00:01.50]顔が", encoding="utf-8")

    # 2) タグなし FLAC(ファイル名とフォルダ名から推測)+ フォルダのカバー
    make_audio(lossless / "Marrow & Fern" / "Tessellate [2024]" / "03 - Glass Orchard.flac")
    from PIL import Image
    Image.new("RGB", (300, 300), (30, 90, 200)).save(lossless / "Marrow & Fern" / "Tessellate [2024]" / "folder.jpg")

    # 3) 文字化けタグの MP3(CP932 を latin-1 として書いたもの)
    p = make_audio(lossy / "single" / "track.mp3", codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    t = ID3()
    bad = lambda s: s.encode("cp932").decode("latin-1")
    t.add(TIT2(encoding=Encoding.LATIN1, text=bad("朝焼け前")))
    t.add(TPE1(encoding=Encoding.LATIN1, text=bad("潮見 すず")))
    t.add(TALB(encoding=Encoding.LATIN1, text=bad("海沿いの手紙")))
    t.add(TRCK(encoding=Encoding.LATIN1, text="4"))
    t.add(TDRC(encoding=Encoding.UTF8, text="2020"))
    t.save(p)

    # 4) TTA(ブラウザで直接再生できない形式)
    make_audio(lossless / "Other" / "Album" / "01 tta song.tta", codec_args=("-c:a", "tta"))

    # 5) 隠しフォルダ(スキャンしない)
    make_audio(lossless / "@eaDir" / "x.flac")
    return {"lossless": lossless, "lossy": lossy, "root": tmp_path}


@pytest.fixture
def cfg(library, tmp_path):
    from otokura.config import Config, Source
    return Config(sources=[Source("lossless", "Lossless", library["lossless"]), Source("lossy", "1.Lossy", library["lossy"])],
                  data_dir=tmp_path / "data", scan_at=None, scan_on_start=False, lrclib=False,
                  web_dir=Path(__file__).resolve().parents[2] / "web")
