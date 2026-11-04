import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import HAS_FFMPEG
from otokura import chapters

META = """;FFMETADATA1
title=Live at Budokan
artist=潮見 すず
[CHAPTER]
TIMEBASE=1/1000
START=0
END=3000
title=オープニング
[CHAPTER]
TIMEBASE=1/1000
START=3000
END=6000
title=海沿いの手紙
[CHAPTER]
TIMEBASE=1/1000
START=6000
END=9000
title=アンコール
"""


def make_video(path, meta=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=9",
           "-f", "lavfi", "-i", "color=c=red:s=64x64:r=5:d=9"]
    if meta:
        m = path.with_suffix(".txt")
        m.write_text(meta, encoding="utf-8")
        cmd += ["-i", str(m), "-map_metadata", "2", "-map_chapters", "2"]
    subprocess.run(cmd + ["-map", "0:a", "-map", "1:v", "-c:a", "aac", "-c:v", "mpeg4", str(path)], check=True)
    return path


def test_parse_needs_audio_and_two_chapters(tmp_path):
    p = tmp_path / "x.mp4"
    ch = [{"start_time": "0", "end_time": "5", "tags": {"title": "A"}}, {"start_time": "5", "end_time": "9"}]
    sheet, tracks = chapters.parse({"streams": [{"codec_type": "audio"}], "chapters": ch, "format": {"tags": {"title": "T"}}}, p)
    assert sheet.title == "T" and [(t.no, t.title, t.start_ms, t.end_ms) for t in tracks] == [(1, "A", 0, 5000), (2, None, 5000, None)]
    assert chapters.parse({"streams": [{"codec_type": "video"}], "chapters": ch}, p) is None      # 音声が無い
    assert chapters.parse({"streams": [{"codec_type": "audio"}], "chapters": ch[:1]}, p) is None  # 1 つだけなら分けない
    zero = [{"start_time": "1", "end_time": "1"}, *ch[:1]]
    assert chapters.parse({"streams": [{"codec_type": "audio"}], "chapters": zero}, p) is None    # 長さ 0 は数えない


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg が必要")
def test_scan_and_stream_chapters(tmp_path):
    from mutagen.mp4 import MP4
    from otokura.app import create_app
    from otokura.config import Config, Source
    make_video(tmp_path / "m" / "潮見 すず" / "live.mp4", META)
    make_video(tmp_path / "m" / "潮見 すず" / "plain.mp4")      # チャプターの無い映像は 1 曲のまま
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None,
                 scan_on_start=False, lrclib=False, webinfo=False)
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    c = TestClient(app)
    j = c.get("/api/library").json()
    tracks = [dict(zip(j["trackColumns"], r)) for r in j["tracks"]]
    albums = [dict(zip(j["albumColumns"], r)) for r in j["albums"]]
    cut = [t for t in tracks if t["cue"]]
    assert [(t["no"], t["title"]) for t in cut] == [(1, "オープニング"), (2, "海沿いの手紙"), (3, "アンコール")]
    assert all(abs(t["durationMs"] - 3000) < 100 for t in cut)
    assert sum(1 for t in tracks if not t["cue"]) == 1
    live = next(a for a in albums if a["title"] == "Live at Budokan")      # フォルダ名ではなくファイルのタイトルがアルバムになる
    assert live["hasArt"]                                                   # 映像の 1 コマをカバーにする
    r = c.get(f"/api/tracks/{cut[1]['id']}/stream")
    assert r.headers["content-type"] == "audio/mp4"
    out = tmp_path / "x.m4a"
    out.write_bytes(r.content)
    assert abs(MP4(out).info.length - 3) < 0.2
    ids = [t["id"] for t in tracks]
    st = app.state.scanner.run()                                            # 2 回目は読み直さず同じ曲のまま
    j = c.get("/api/library").json()
    assert sorted(r[0] for r in j["tracks"]) == sorted(ids) and st.added == 0 and st.removed == 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg が必要")
def test_upgrade_rescans_old_rows_once(tmp_path):
    # 以前のスキャンで 1 曲として登録済みのファイルも、SCAN_REV が上がったら読み直してチャプターで分ける
    from otokura.config import Config, Source
    from otokura.db import Database
    from otokura.scanner import Scanner
    make_video(tmp_path / "m" / "a" / "live.mp4", META)
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None, scan_on_start=False, lrclib=False)
    db = Database(cfg.db_path)
    s = Scanner(db, cfg)
    p = tmp_path / "m" / "a" / "live.mp4"
    db.conn.execute("INSERT INTO tracks(source,rel,mtime,size,title,artist,added_at) VALUES('m','a/live.mp4',?,?, 'old','x',0)",
                    (p.stat().st_mtime, p.stat().st_size))
    db.conn.commit()
    s.run()
    assert [r["rel"] for r in db.q("SELECT rel FROM tracks ORDER BY rel")] == ["a/live.mp4#01", "a/live.mp4#02", "a/live.mp4#03"]
    assert db.meta("scan_rev") == "2"
