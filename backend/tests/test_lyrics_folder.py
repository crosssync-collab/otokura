from otokura.lyrics.folder import LyricsFolder


def write(p, text, enc="utf-8"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", "\r\n").encode(enc))


def test_find_ignores_case_width_brackets_and_feat(tmp_path):
    write(tmp_path / "GReeeeN - あの日のオレンジ.txt", "x")
    write(tmp_path / "lyrics" / "Mr.Children - 跳べ [Live].txt", "x")
    write(tmp_path / "Kygo - Here For You (Feat. Ella Henderson).lrc", "x")
    write(tmp_path / "メモ.txt", "名前に区切りが無いものは使わない")
    f = LyricsFolder([tmp_path])
    assert [p.name for p in f.find("greeeen", "あの日のオレンジ")] == ["GReeeeN - あの日のオレンジ.txt"]
    assert f.find("Mr.Children", "跳べ")[0].parent.name == "lyrics"          # 中のフォルダ・括弧書きなし
    assert f.find("Ｍｒ．Ｃｈｉｌｄｒｅｎ", "跳べ [Live]")                     # 全角
    assert f.find("Kygo feat. Ella Henderson", "Here For You")               # feat. の有無
    assert f.find("別の人", "あの日のオレンジ") == [] and f.find(None, "x") == []


def test_load_strips_header_and_prefers_lrc(tmp_path):
    write(tmp_path / "B'z - FIREBALL.txt",
          "FIREBALL\n\n歌手　B'z\n作詞　稲葉浩志\n作曲　松本孝弘\n\n一行目の歌詞\n二行目の歌詞\n", "utf-8-sig")
    f = LyricsFolder([tmp_path])
    ly = f.load(f.find("B'z", "FIREBALL"), "FIREBALL")
    assert ly.source == "folder:txt" and not ly.synced
    assert [l.text for l in ly.main.lines] == ["一行目の歌詞", "二行目の歌詞"]

    write(tmp_path / "B'z - FIREBALL.lrc", "[ti:FIREBALL]\n[00:12.00]一行目の歌詞\n[00:15.00]二行目の歌詞\n")
    f.checked = 0     # すぐ作り直させる
    ly = f.load(f.find("B'z", "FIREBALL"), "FIREBALL", 200000)
    assert ly.source == "folder:lrc" and ly.synced and ly.main.lines[0].start == 12000


def test_load_strips_header_in_lrc(tmp_path):
    write(tmp_path / "Mr.Children - ためいきの日曜日.lrc",
          "[00:00.00]ためいきの日曜日 (嘆息的星期日)\n[00:02.00]Mr.Children\n[00:04.00]作詞：桜井和寿\n"
          "[00:06.00]Album:Best\n[00:20.00]最初の歌詞\n[00:25.00]作曲の話をする歌詞\n")
    f = LyricsFolder([tmp_path])
    ly = f.load(f.find("Mr.Children", "ためいきの日曜日"), "ためいきの日曜日", 200000, "Mr.Children")
    assert [l.text for l in ly.main.lines] == ["最初の歌詞", "作曲の話をする歌詞"]
    assert ly.main.lines[0].start == 20000


def test_index_follows_new_files(tmp_path):
    f = LyricsFolder([tmp_path])
    assert f.find("A", "B") == []
    write(tmp_path / "A - B.txt", "歌詞")
    f.checked = 0
    assert len(f.find("A", "B")) == 1
