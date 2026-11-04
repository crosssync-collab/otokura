# 音蔵 設計メモ(v0.2:土台+スキャン+再生+変換配信)

## 全体

```
ブラウザ(web/index.html) ──HTTP──▶ FastAPI(otokura.app) ──▶ SQLite(/data/otokura.db)
        ▲  <audio> で直接再生           │                      ▲
        └──── /api/tracks/{id}/stream ◀─┤ スキャナ(otokura.scanner)┘
                                        ├─ /music/* (読み書きできるマウント)
                                        └─ ffmpeg(otokura.transcode)→ /data/transcode
```

- 1 コンテナ、1 プロセス(uvicorn)。スキャンはバックグラウンドのスレッド。
- 音楽ファイルに書き込むのは、利用者がプロパティでタグ・カバーを編集したときだけ(tagedit.py / tag_api.py)。書く前の値を tag_edits と data/tag-history に残し、元に戻せる。スキャン・再生・歌詞では書き込まない。
- 状態はすべて `/data` に置く(DB、カバーの縮小画像、変換したファイル、タグ編集の履歴)。コンテナを作り直しても消えない。

## ソース

`OTOKURA_SOURCES` に `名前=コンテナ内のパス` をカンマ区切りで書く。

```
OTOKURA_SOURCES=Lossless=/music/Lossless,1.Lossy=/music/1.Lossy
```

## スキャン

1. ソースごとにフォルダを歩き、対応拡張子のファイルを集める。
2. DB の (パス, 更新時刻, サイズ) と比べ、変わったものだけタグを読む(差分スキャン)。消えたファイルは DB から外す。
3. タグは mutagen で読む。文字列は CP932 文字化けの修復を通す。
4. タグが無い曲は、ファイル名(`01 曲名.flac`、`01 - アーティスト - 曲名.flac`)とフォルダ名(`アーティスト/アルバム (年)/`)から補う。
5. アルバムは (アルバムアーティスト, アルバム名, ソース) でまとめる。アルバム名が無ければフォルダ単位。
6. カバーは 埋め込み画像 → フォルダの `cover/folder/front.jpg|png` の順で探し、300px と 1000px の縮小画像を `/data/art` に作る。主な色の色相を求めて画面の配色に使う。
7. 歌詞は歌詞モジュール(`otokura.lyrics`)の差分更新を曲ごとに呼ぶ。
8. CUE シート(`otokura.cue`): 同じフォルダの `.cue`(UTF-8 / CP932)で FILE の名前が同じ(拡張子違いも可)音声ファイル、またはタグに埋め込まれた CUESHEET(TAK・APE・FLAC。FILE 名は見ず、いちばん曲の多い FILE)で 2 曲以上に分かれるものは、`元のファイル#曲番号` の曲として登録し、`tracks.cue_start / cue_end` に区間を持つ。元のファイル自体は曲として出さない。曲名・歌い手・アルバム・年・ジャンル・ディスク番号は CUE から。曲ごとのファイル用の `.cue`(EAC の Noncompliant)は INDEX 01 のある FILE の曲として数えるので分けない。
9. TAK は mutagen が読めないので、タグは APEv2、長さ・サンプルレートは ffprobe で読む。

既定は毎日 03:00 に差分スキャン(`OTOKURA_SCAN_AT`)。設定画面の「今すぐスキャン」でも走る。

## DB(主なテーブル)

| テーブル | 内容 |
| --- | --- |
| `tracks` | パス、更新時刻、タグ情報、長さ、形式、アルバム ID、CUE の区間(cue_start / cue_end) |
| `albums` | 名前、アルバムアーティスト、年、ジャンル、ソース、フォルダ、カバーの有無と色相、追加日時 |
| `track_stats` | 再生回数、最終再生、スキップ回数、お気に入り、評価(スキャンで消えない) |
| `playlists` / `playlist_items` | 通常のプレイリスト(曲は相対パスでも保持し、再スキャンで ID が変わっても戻せる) |
| `settings` | 画面の設定(JSON) |
| `lyrics_local` / `lyrics_user` | 歌詞(歌詞モジュール)。lyrics_user.manual_prev に手動の歌詞の 1 つ前の版 |
| `tag_edits` | タグ編集の履歴(元に戻す用。画像は data/tag-history) |

## API

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/api/library` | 画面が最初に読む一覧(アルバム・曲・統計)。ETag 付き |
| GET | `/api/tracks/{id}/stream` | 音声の配信(Range 対応)。鳴らせない形式・CUE の曲は変換して配る。`?direct=1` その端末がそのまま鳴らせる(Safari の ALAC 等)、`?cast=1` Google Cast 向け(48kHz まで) |
| POST | `/api/tracks/{id}/prepare` | 次の曲を先に変換しておく |
| GET/PUT | `/api/tracks/{id}/meta`、`/api/albums/{id}/meta`、`.../cover` | タグ・カバーの表示と編集。`POST /api/tagedits/{id}/revert` で元に戻す |
| GET | `/mix/{name}.jpg` | ミックス・ショートカットの写真 |
| GET | `/api/art/{album_id}?size=300` | カバー画像 |
| POST | `/api/tracks/{id}/played` / `skipped` | 再生記録 |
| PUT | `/api/tracks/{id}/stats` | お気に入り・評価 |
| GET/POST/PUT/DELETE | `/api/playlists...` | プレイリスト |
| GET/PUT | `/api/settings` | 画面の設定 |
| GET | `/api/server` | サーバー情報、ソース、スキャン状態 |
| POST | `/api/scan` | 今すぐスキャン |
| (歌詞) | `/api/tracks/{id}/lyrics...` | 歌詞モジュール。`POST .../lyrics/restore` で手動の歌詞を前の版に戻す |

## 画面

モックの描画コードはそのまま使い、データの入り口だけを差し替える。

- 起動時に `/api/library` を読み、モックと同じ形の `AL`(アルバム)と `SONGS`(曲)を作る。画面内の番号は配列の位置で、サーバーの ID は `dbid` に持つ。
- 再生は `<audio>`。再生位置・再生中かどうかは audio 要素から取る。
- ミックスは今まで通りブラウザで計算する(再生回数・お気に入りはサーバーから届く)。
- 再生回数は、曲の半分か 4 分を過ぎたら記録。30 秒より前に次へ進んだらスキップとして記録。

## 変換配信(otokura.transcode)

- 可逆(ALAC・AIFF・APE・TTA・WavPack・TAK・DSD)→ FLAC、非可逆(WMA・MPC)→ AAC 256kbps。DSD は PCM 24bit・88.2kHz
- CUE の曲は `-ss`(入力の前)と `-t` で区間を切り出す。可逆は FLAC、MP3 などは AAC
- キャッシュのキーは 元のパス + 更新時刻 + 大きさ + Cast 向けか + 区間。同じものを同時に 2 つ変換しない。上限(`OTOKURA_TRANSCODE_MAX_GB`)を超えたら使った日時の古いものから消す
- 変換しながら流す方式はとらない(途中から再生できず、長さも分からないため)。初回だけ変換を待つ(ALAC 1 曲 0.5 秒、TAK の途中の曲 5 秒、WMA→AAC 11 秒)。画面は次の曲を `/prepare` で先に変換させる

## 次の段階

- DLNA へのキャスト
- アーティスト画像やアルバム情報の Web 取得
- ReplayGain、ギャップレス、スマートフェード(設定項目は残してある)
- MP4 のチャプターで曲に分ける
