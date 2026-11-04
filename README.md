# 音蔵(おとくら)

NAS の音楽フォルダを、ブラウザから聴くための音楽プレイヤーです。Symfonium の使い心地を参考にしています。UGREEN NAS などの Docker で常時起動し、Web ダッシュボードから操作します。

- ライブラリ: アルバム / アーティスト / ジャンル / 曲 / フォルダ、検索、ソースの絞り込み。ホームのショートカットとミックスは写真のジャケット
- ミックス: 曲・アルバム・年代・パーソナル・「この曲に近い曲」
- 再生形式: ブラウザが鳴らせない ALAC・AIFF・APE・TTA・WavPack・TAK・DSD・WMA・MPC は NAS で変換して配る。CUE シート(別ファイル・タグ埋め込み)のアルバムは曲ごとに分ける
- 歌詞: LRC / 語ごとの時刻つき LRC / TTML / 埋め込みタグ / 歌詞フォルダ(`アーティスト - 曲名.lrc`)/ LRCLIB。語ごとの同期表示、空白行(段落)は待機の行、時刻の無い歌詞は時刻を推定
  - ↑↓ はその場だけのずれ調整。タイミング調整を長押し(または「歌詞調整」を 3 回押す)で編集モード: 出だしの位置の保存、または曲を最初から流して「次の行」「ここから間奏」で行ごとに記録。保存前の版に 1 回戻せる
  - 横向き(スマホ・PC)は 写真 → 歌詞 → 全画面の歌詞(背景にジャケット)を「歌詞」ボタンで切り替え
- 日本語タグの文字化け(CP932)を自動で直して表示。タグの無いファイルはファイル名とフォルダ名から補う
- 再生回数・お気に入り・評価・プレイリスト・設定はサーバーに保存
- 曲・アルバムのプロパティ: タグ(基本の項目・すべてのタグ)・カバー画像を表示・編集して音楽ファイルに書く。書く前の値を残すので元に戻せる。ファイルの情報も表示
- 再生先: このブラウザ、Google Cast(Chromecast・Nest。ハイレゾは 48kHz にして送る)、DLNA・UPnP(ネットワークプレーヤー・AV アンプ。Docker のブリッジのままで探せる。IP アドレスの手入力も可)
- 音量とつなぎ: ReplayGain(タグが無い曲は NAS で測る)、ギャップレス(アルバムの続きの曲)、スマートフェード(別のアルバムへ。無音を飛ばして重ねる)。iPhone では効かない
- Web の情報: アーティスト写真・紹介文、アルバムの発売日・レーベル・紹介文(MusicBrainz・Wikipedia・Deezer・iTunes)。30 日経ったものは少しずつ調べ直す
- この端末の曲: PC のフォルダをそのまま読む、またはスマホに取り込む(NAS を通さずその端末だけで再生)
- ホーム画面に追加してアプリとして使える(全画面・画面の向きの固定・画面を消さない)

設計は [docs/DESIGN.md](docs/DESIGN.md) を参照。

## NAS で動かす(SSH + docker compose)

```sh
ssh <ユーザー>@<NAS のアドレス>
git clone https://github.com/crosssync-collab/otokura.git
cd otokura
cp .env.example .env
id                      # uid と gid を確認して .env の PUID / PGID に書く
vi .env                 # MUSIC_LOSSLESS / MUSIC_LOSSY を NAS 上の実際のパスに
mkdir -p data
docker compose up -d --build
docker compose logs -f  # 「スキャン完了」が出れば準備完了
```

ブラウザで `http://<NAS のアドレス>:8686/` を開きます。スマホのホーム画面に追加してアプリとして使うには HTTPS が要るので、Tailscale を使うなら NAS で `sudo tailscale serve --bg --https=8443 8686` を実行し、`https://<NAS の名前>.<tailnet>.ts.net:8443/` を開きます。初回は全曲のタグを読むので、1 万曲で数分かかります。2 回目からは変わったファイルだけを読みます(毎日 03:00 と、設定 → メディアプロバイダ →「今すぐスキャン」)。

更新するとき:

```sh
cd otokura && git pull && docker compose up -d --build
```

データ(DB、カバーの縮小画像、変換したファイル)は `.env` の `OTOKURA_DATA_DIR`(既定は `./data`)に残るので、作り直しても再生回数などは消えません。

## 設定(環境変数)

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `OTOKURA_SOURCES` | `Lossless=/music/Lossless,1.Lossy=/music/1.Lossy` | 画面に出す名前=コンテナ内のパス。カンマ区切り |
| `OTOKURA_SCAN_AT` | `03:00` | 毎日の差分スキャン時刻。空で無効 |
| `OTOKURA_SCAN_ON_START` | `1` | 起動時にスキャンする |
| `OTOKURA_LRCLIB` | `1` | 歌詞が無い曲だけ LRCLIB から探す |
| `OTOKURA_DATA` | `/data` | データの置き場所(コンテナ内) |
| `OTOKURA_LYRICS` | (なし) | 歌詞フォルダ(コンテナ内のパス、カンマ区切り)。横にもタグにも歌詞が無い曲は、ここの `アーティスト - 曲名.lrc / .txt` を探す |
| `OTOKURA_CAST_URL` | (なし) | Google Cast・DLNA の機器が曲を取りに来る、家の中の NAS のアドレス(例 `http://192.168.0.252:8686`)。DLNA の機器を探す範囲にも使う |
| `OTOKURA_WEBINFO` | `1` | アーティスト画像・アルバム情報を Web から取る。`0` で無効 |
| `OTOKURA_TRANSCODE_MAX_GB` | `20` | 変換したファイルを残しておく上限(GB)。超えたら長く使っていないものから消す |

## 対応形式

| 形式 | 再生 |
| --- | --- |
| FLAC / MP3 / AAC(M4A)/ Ogg Vorbis / Opus / WAV | そのまま配信 |
| ALAC / AIFF / APE / TTA / WavPack / TAK | NAS で FLAC に変換して配信(ALAC・AIFF は Safari ならそのまま) |
| DSD(DSF / DFF) | PCM 24bit・88.2kHz の FLAC に変換して配信 |
| WMA / MPC | AAC 256kbps に変換して配信 |
| CUE シート + 1 ファイルのアルバム | `.cue`、またはタグに埋め込まれた CUE シート(TAK・APE・FLAC)で曲に分け、その区間だけ切り出して配信 |
| チャプター付きの MP4・M4A(ライブ映像など) | チャプターで曲に分け、音声の区間だけ切り出して配信(カバーが無ければ映像の 1 コマ) |

- 変換には Docker イメージに入っている ffmpeg を使う。一度変換したものは `data/transcode` に残すので、2 回目からはすぐ鳴り、途中から再生もできる。次の曲は先に変換を始める
- Google Cast には、48kHz を超える曲を 24bit・48kHz の FLAC にして送る
- 曲ごとのファイルに付いている `.cue`(EAC の Noncompliant 形式など)は使わない。CUE の曲はタグを編集できない(`.cue` を書き換えて再スキャン)

## 開発

```sh
cd backend
pip install -r requirements.txt pytest
python -m pytest -q          # ffmpeg があればタグ・スキャン・API の通しテストも走る
OTOKURA_SOURCES="Music=/path/to/music" OTOKURA_DATA=./data uvicorn otokura.app:app --reload --port 8686
```

`web/index.html` は 1 ファイルの画面です。サーバーから開くとライブラリにつながり、ファイルを直接開くとデモデータで動きます。API の一覧は `http://<アドレス>:8686/api/docs`。

## 写真・ライブラリのライセンス

- ミックスとホームのショートカットの写真は Unsplash(Unsplash License)。撮影者は `web/mix/credits.json`
- タグの読み書きに mutagen(GPL)、変換に ffmpeg(Debian のパッケージ)を使う
