from otokura.lyrics import parse_lrc, parse_plain, parse_ttml
from otokura.lyrics.encoding import decode_bytes, fix_mojibake
from otokura.lyrics.ttml import parse_time


# ---------- LRC ----------
def test_basic_lrc_and_tags():
    ly = parse_lrc("[ar:潮見 すず]\n[ti:手紙]\n[00:01.00]一行目\n[00:04.50]二行目\n[00:09.123]三行目\n", track_ms=12000)
    m = ly.main
    assert ly.synced and not ly.has_words
    assert [l.start for l in m.lines] == [1000, 4500, 9123]
    assert [l.end for l in m.lines] == [4500, 9123, 12000]
    assert m.lines[0].text == "一行目"


def test_paragraph_breaks_kept():
    # 時刻だけの行 = 待機の行(前の行の終わりから次の行まで)、時刻の無い空白行 = 長さ 0 の区切り。先頭・末尾・連続は入れない
    ly = parse_lrc("[00:00.50]\n[00:01.00]A\n[00:03.00]\n[00:03.50]\n[00:08.00]B\n\n\n[00:10.00]C\n[00:12.00]\n", track_ms=20000)
    assert [(l.text, l.start, l.end) for l in ly.main.lines] == [
        ("A", 1000, 3000), ("", 3000, 8000), ("B", 8000, 10000), ("", 10000, 10000), ("C", 10000, 12000)]


def test_time_variants():
    ly = parse_lrc("[1:02]a\n[01:03.5]b\n[01:04:20]c\n[01:05.005]d")
    assert [l.start for l in ly.main.lines] == [62000, 63500, 64200, 65005]


def test_repeated_stamps_and_order():
    ly = parse_lrc("[00:10.00][00:30.00]サビ\n[00:20.00]Aメロ")
    assert [(l.start, l.text) for l in ly.main.lines] == [(10000, "サビ"), (20000, "Aメロ"), (30000, "サビ")]


def test_offset_positive_shows_earlier():
    ly = parse_lrc("[offset:+500]\n[00:10.00]x\n[00:12.00]y")
    assert ly.main.lines[0].start == 9500
    assert ly.main.offset_ms == 500


def test_blank_line_marks_end_for_interlude():
    ly = parse_lrc("[00:10.00]歌\n[00:14.00]\n[00:40.00]また歌")
    a, gap, b = ly.main.lines
    assert (a.start, a.end) == (10000, 14000)
    assert (gap.text, gap.start, gap.end) == ("", 14000, 40000)
    assert b.start == 40000


def test_enhanced_lrc_words():
    ly = parse_lrc("[00:12.00]<00:12.00>Hello <00:12.50>world<00:13.20>\n[00:14.00]<00:14.00>夜<00:14.30>明<00:14.60>け")
    l0, l1 = ly.main.lines
    assert ly.has_words
    assert l0.text == "Hello world"
    assert [(w.start, w.end, w.text) for w in l0.words] == [(12000, 12500, "Hello "), (12500, 13200, "world")]
    assert [w.text for w in l1.words] == ["夜", "明", "け"]
    assert l1.words[-1].end == l1.end


def test_agents_and_bg():
    ly = parse_lrc("[00:01.00]v1: わたし\n[bg: <00:01.50>(ah)<00:02.00>]\n[00:03.00]v2: あなた")
    l0, l1 = ly.main.lines
    assert l0.agent == "v1" and l1.agent == "v2"
    assert l0.bg_text == "(ah)"
    assert {a.id: a.role for a in ly.main.agents} == {"v1": "main", "v2": "duet"}


def test_translation_by_duplicate_stamp():
    ly = parse_lrc("[00:01.00]Hello\n[00:01.00]こんにちは\n[00:03.00]World\n[00:03.00]世界")
    assert [l.text for l in ly.main.lines] == ["Hello", "World"]
    tr = [L for L in ly.layers if L.kind == "translation"][0]
    assert [l.text for l in tr.lines] == ["こんにちは", "世界"]


def test_unsynced_lrc_like_text():
    ly = parse_lrc("ただの歌詞\nもう一行")
    assert not ly.synced and [l.text for l in ly.main.lines] == ["ただの歌詞", "もう一行"]


def test_plain_keeps_paragraphs():
    ly = parse_plain("\n\nA\nB\n\n\nC\n\n")
    assert [l.text for l in ly.main.lines] == ["A", "B", "", "C"]


# ---------- TTML ----------
APPLE = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"
    xmlns:itunes="http://music.apple.com/lyric-ttml-internal" xml:lang="ja">
 <head><metadata>
  <ttm:agent type="person" xml:id="v1"><ttm:name type="full">A</ttm:name></ttm:agent>
  <ttm:agent type="person" xml:id="v2"/>
  <iTunesMetadata xmlns="http://music.apple.com/lyric-ttml-internal">
   <translations><translation type="replacement" xml:lang="en">
     <text for="L1">Night bus</text></translation></translations>
   <transliterations><transliteration xml:lang="ja-Latn">
     <text for="L1">yakou basu</text></transliteration></transliterations>
  </iTunesMetadata>
 </metadata></head>
 <body dur="1:30.000"><div>
  <p begin="0:10.000" end="0:12.000" itunes:key="L1" ttm:agent="v1"><span begin="0:10.000" end="0:10.500">夜</span><span begin="0:10.500" end="0:11.000">行</span><span begin="0:11.000" end="0:12.000">バス</span><span ttm:role="x-bg"><span begin="0:11.200" end="0:11.800">(ah)</span></span></p>
  <p begin="0:13.000" end="0:15.000" itunes:key="L2" ttm:agent="v2"><span begin="0:13.000" end="0:14.000">Hello</span> <span begin="0:14.000" end="0:15.000">there</span></p>
 </div></body></tt>"""


def test_ttml_apple():
    ly = parse_ttml(APPLE)
    m = ly.main
    assert m.lang == "ja" and ly.synced and ly.has_words
    l0, l1 = m.lines
    assert l0.text == "夜行バス" and l0.agent == "v1"
    assert [w.text for w in l0.words] == ["夜", "行", "バス"]
    assert l0.bg_text == "(ah)" and l0.bg[0].start == 11200
    assert l1.text == "Hello there" and l1.words[0].text == "Hello "
    assert {a.id for a in m.agents} == {"v1", "v2"}
    kinds = {L.kind: L for L in ly.layers}
    assert kinds["translation"].lines[0].text == "Night bus"
    assert kinds["pronunciation"].lines[0].start == 10000


def test_ttml_pretty_printed_japanese_has_no_spaces():
    x = """<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
      <p begin="1s" end="3s">
        <span begin="1s" end="1.5s">朝</span>
        <span begin="1.5s" end="2s">焼</span>
        <span begin="2s" end="3s">け</span>
      </p></div></body></tt>"""
    assert parse_ttml(x).main.lines[0].text == "朝焼け"


def test_ttml_line_only():
    x = '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="00:00:01.5" end="00:00:03">一行</p><p begin="4s">二行</p></div></body></tt>'
    ly = parse_ttml(x, track_ms=9000)
    assert not ly.has_words
    assert [(l.start, l.end) for l in ly.main.lines] == [(1500, 3000), (4000, 9000)]


def test_parse_time():
    assert parse_time("1:02.5") == 62500
    assert parse_time("01:00:00.000") == 3600000
    assert parse_time("250ms") == 250
    assert parse_time("1.25s") == 1250


# ---------- 文字コード ----------
def test_decode_cp932_file():
    assert decode_bytes("夜行バス".encode("cp932")) == "夜行バス"
    assert decode_bytes("﻿夜".encode("utf-8")) == "夜"


def test_fix_mojibake():
    broken = "夜行バス".encode("cp932").decode("latin-1")
    assert fix_mojibake(broken) == "夜行バス"
    assert fix_mojibake("Beyoncé") == "Beyoncé"     # 正当なラテン文字は壊さない
    assert fix_mojibake("夜行バス") == "夜行バス"


def test_to_json_roundtrip():
    from otokura.lyrics.model import Lyrics
    ly = parse_ttml(APPLE)
    back = Lyrics.from_json(ly.to_json())
    assert back.to_json() == ly.to_json()
