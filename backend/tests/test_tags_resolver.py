import shutil
import subprocess
from pathlib import Path

import pytest

from otokura.lyrics.resolver import resolve_local
from otokura.lyrics.tags import read_tag_lyrics

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg が必要")


def make_audio(path: Path, seconds=10):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r=44100:cl=mono", "-t", str(seconds), str(path)], check=True)
    return path


def test_flac_vorbis_lyrics_lrc(tmp_path):
    from mutagen.flac import FLAC
    p = make_audio(tmp_path / "a.flac")
    f = FLAC(p); f["LYRICS"] = "[00:01.00]一\n[00:03.00]二"; f.save()
    (ly,) = read_tag_lyrics(p)
    assert ly.source == "tag:lrc" and ly.synced
    assert ly.main.lines[-1].end == 10000   # 曲の長さで閉じる


def test_mp3_sylt_and_uslt_mojibake(tmp_path):
    from mutagen.id3 import ID3, SYLT, USLT, Encoding
    p = make_audio(tmp_path / "b.mp3")
    t = ID3()
    t.add(SYLT(encoding=Encoding.UTF8, lang="jpn", format=2, type=1, text=[("夜", 1000), ("明け", 2500)]))
    broken = "朝焼け".encode("cp932").decode("latin-1")
    t.add(USLT(encoding=Encoding.LATIN1, lang="jpn", desc="", text=broken))
    t.save(p)
    found = read_tag_lyrics(p)
    srcs = {l.source: l for l in found}
    assert [l.text for l in srcs["tag:sylt"].main.lines] == ["夜", "明け"]
    assert srcs["tag:text"].main.lines[0].text == "朝焼け"


def test_m4a_lyr(tmp_path):
    from mutagen.mp4 import MP4
    p = tmp_path / "c.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "5", "-c:a", "aac", str(p)], check=True)
    m = MP4(p); m["\xa9lyr"] = ["ふつうの歌詞"]; m.save()
    (ly,) = read_tag_lyrics(p)
    assert not ly.synced and ly.main.lines[0].text == "ふつうの歌詞"


def test_resolver_prefers_synced_sidecar_over_plain_tag(tmp_path):
    from mutagen.flac import FLAC
    p = make_audio(tmp_path / "曲.flac")
    f = FLAC(p); f["UNSYNCEDLYRICS"] = "タグの歌詞"; f.save()
    (tmp_path / "曲.LRC").write_bytes("[00:01.00]サイドカー".encode("cp932"))   # 大文字拡張子 + CP932
    ly, watched = resolve_local(p)
    assert ly.source == "sidecar:lrc" and ly.main.lines[0].text == "サイドカー"
    assert len(watched) == 1


def test_resolver_prefers_word_sync(tmp_path):
    p = make_audio(tmp_path / "x.flac")
    (tmp_path / "x.lrc").write_text("[00:01.00]行だけ\n[00:02.00]次", encoding="utf-8")
    (tmp_path / "x.ttml").write_text(
        '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="1s" end="2s"><span begin="1s" end="2s">語</span></p></div></body></tt>',
        encoding="utf-8")
    ly, _ = resolve_local(p)
    assert ly.source == "sidecar:ttml" and ly.has_words


def test_resolver_none(tmp_path):
    p = make_audio(tmp_path / "n.flac")
    ly, watched = resolve_local(p)
    assert ly is None and watched == []
