import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from otokura import cue
from conftest import HAS_FFMPEG

SHEET = '''REM GENRE "J-Pop"
REM DATE 2004
PERFORMER "潮見 すず"
TITLE "海沿いの手紙"
FILE "image.wav" WAVE
  TRACK 01 AUDIO
    TITLE "朝"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "昼"
    PERFORMER "ゲスト"
    INDEX 00 00:02:50
    INDEX 01 00:03:00
  TRACK 03 AUDIO
    TITLE "夜"
    INDEX 01 00:06:37
'''


def test_parse():
    cs = cue.parse(SHEET)
    assert (cs.title, cs.performer, cs.date, cs.genre) == ("海沿いの手紙", "潮見 すず", "2004", "J-Pop")
    t = cs.files["image.wav"]
    assert [(x.no, x.title, x.start_ms, x.end_ms) for x in t] == [(1, "朝", 0, 3000), (2, "昼", 3000, 6493), (3, "夜", 6493, None)]
    assert t[1].performer == "ゲスト" and t[0].performer is None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg が必要")
def test_scan_stream_and_edit(tmp_path):
    from mutagen.flac import FLAC
    from otokura.app import create_app
    from otokura.config import Config, Source
    d = tmp_path / "m" / "潮見 すず" / "海沿いの手紙"
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "9", str(d / "image.flac")], check=True)
    (d / "image.cue").write_bytes(SHEET.encode("cp932"))       # 日本の取り込みに多い CP932
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None,
                 scan_on_start=False, lrclib=False)
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    c = TestClient(app)

    def lib():
        j = c.get("/api/library").json()
        return [dict(zip(j["trackColumns"], r)) for r in j["tracks"]], [dict(zip(j["albumColumns"], r)) for r in j["albums"]]

    tracks, albums = lib()
    assert [(t["no"], t["title"], t["artist"], t["cue"]) for t in tracks] == [
        (1, "朝", "潮見 すず", 1), (2, "昼", "ゲスト", 1), (3, "夜", "潮見 すず", 1)]      # 元のファイルは曲として出さない
    assert abs(tracks[0]["durationMs"] - 3000) < 50 and abs(tracks[2]["durationMs"] - 2507) < 80
    assert len(albums) == 1 and albums[0]["title"] == "海沿いの手紙" and albums[0]["year"] == 2004
    # 区間だけを切り出して配る
    r = c.get(f"/api/tracks/{tracks[1]['id']}/stream")
    assert r.headers["content-type"] == "audio/flac"
    out = tmp_path / "x.flac"
    out.write_bytes(r.content)
    assert abs(FLAC(out).info.length - 3.493) < 0.05
    assert c.post(f"/api/tracks/{tracks[2]['id']}/prepare").json()["needed"]
    # タグは編集しない
    assert c.put(f"/api/tracks/{tracks[0]['id']}/meta", json={"fields": {"title": "x"}}).status_code == 409
    # CUE を書き換えたら読み直す
    time.sleep(0.02)
    (d / "image.cue").write_bytes(SHEET.replace("朝", "夜明け").encode("cp932"))
    os.utime(d / "image.cue", (time.time() + 5, time.time() + 5))
    app.state.scanner.run()
    tracks2, _ = lib()
    assert tracks2[0]["title"] == "夜明け" and tracks2[0]["id"] == tracks[0]["id"]      # 同じ曲として(再生記録が残る)
