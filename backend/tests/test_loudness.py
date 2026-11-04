import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import HAS_FFMPEG, make_audio
from otokura import loudness, metadata

LOG = """[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 1.5 | silence_duration: 1.5
[Parsed_ebur128_1 @ 0x2] Summary:
  Integrated loudness:
    I:         -12.3 LUFS
[silencedetect @ 0x1] silence_start: 8
[silencedetect @ 0x1] silence_end: 10 | silence_duration: 2
"""


def test_parse_log():
    assert loudness.parse(LOG, 10) == {"lufs": -12.3, "head_ms": 1500, "tail_ms": 2000}
    assert loudness.parse("nothing", 10) is None
    # 途中の無音は頭にもお尻にも数えない
    mid = "I: -9.0 LUFS\nsilence_start: 4\nsilence_end: 5\n"
    assert loudness.parse(mid, 10) == {"lufs": -9.0, "head_ms": 0, "tail_ms": 0}


def test_replaygain_tag_forms():
    rg = metadata._replaygain
    assert rg({"replaygain_track_gain": ["-6.52 dB"], "replaygain_album_gain": ["+1.5 dB"]}) == (-6.52, 1.5)
    assert rg({"----:com.apple.itunes:replaygain_track_gain": [b"-3.00 dB"]}) == (-3.0, None)
    assert rg({"r128_track_gain": ["-512"]}) == (3.0, None)       # Opus: -2 dB(-23 LUFS 基準)+ 5 dB
    assert rg({}) == (None, None)
    assert rg({"replaygain_track_gain": ["なし"]}) == (None, None)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg が必要")
def test_tags_and_measure_through_api(tmp_path):
    from mutagen.flac import FLAC
    from otokura.app import create_app
    from otokura.config import Config, Source
    root = tmp_path / "m"
    tagged = make_audio(root / "a" / "tagged.flac")
    f = FLAC(tagged)
    f.update({"REPLAYGAIN_TRACK_GAIN": "-4.25 dB", "REPLAYGAIN_ALBUM_GAIN": "-5.00 dB"})
    f.save()
    silent = root / "a" / "untagged.flac"            # 頭 1 秒・お尻 1 秒が無音
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
                    "-af", "adelay=1000:all=1,apad=pad_dur=1", str(silent)], check=True)
    cfg = Config(sources=[Source("m", "M", root)], data_dir=tmp_path / "data", scan_at=None, scan_on_start=False, lrclib=False, webinfo=False)
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    c = TestClient(app)

    def lib():
        j = c.get("/api/library").json()
        return {t["title"]: t for t in (dict(zip(j["trackColumns"], r)) for r in j["tracks"])}
    t = lib()
    assert t["tagged"]["rgTrack"] == -4.25 and t["tagged"]["rgAlbum"] == -5.0
    assert t["untagged"]["rgTrack"] is None
    res = c.post(f"/api/tracks/{t['untagged']['id']}/loudness").json()
    assert res["rgTrack"] is not None and abs(res["headMs"] - 1000) < 150 and abs(res["tailMs"] - 1000) < 150
    t2 = lib()
    assert t2["untagged"]["rgTrack"] == res["rgTrack"] and t2["untagged"]["headMs"] == res["headMs"]    # 一覧にも載る
    # タグがあればタグの値を使う(測った無音は使う)
    res = c.post(f"/api/tracks/{t['tagged']['id']}/loudness").json()
    assert res["rgTrack"] == -4.25
    # まとめて測る: 残りが無ければすぐ終わる。ファイルが変わったら測り直す
    assert app.state.loudness.public()["measured"] == 2
    f = FLAC(tagged)
    f["TITLE"] = "tagged"
    f.save()
    app.state.scanner.run()
    assert app.state.loudness.public()["measured"] == 1
    app.state.loudness.run()
    assert app.state.loudness.public()["measured"] == 2 and app.state.loudness.state["errors"] == 0
