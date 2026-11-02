# 音蔵(おとくら)

NAS の音楽フォルダを、ブラウザから聴くための音楽プレイヤーです。Symfonium の使い心地を参考にしています。UGREEN NAS などの Docker で常時起動し、Web ダッシュボードから操作します。

- ライブラリ: アルバム / アーティスト / ジャンル / 曲 / フォルダ、検索、ソースの絞り込み
- ミックス: 曲・アルバム・年代・パーソナル・「この曲に近い曲」
- 歌詞: LRC / 語ごとの時刻つき LRC / TTML / 埋め込みタグ、語ごとの同期表示、ずれ補正、LRCLIB からの取得
- 日本語タグの文字化け(CP932)を自動で直して表示。タグの無いファイルはファイル名とフォルダ名から補う
- 再生回数・お気に入り・評価・プレイリスト・設定はサーバーに保存。音楽ファイルには書き込まない

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

ブラウザで `http://<NAS のアドレス>:8686/` を開きます。初回は全曲のタグを読むので、1 万曲で数分かかります。2 回目からは変わったファイルだけを読みます(毎日 03:00 と、設定 → メディアプロバイダ →「今すぐスキャン」)。

更新するとき:

```sh
cd otokura && git pull && docker compose up -d --build
```

データ(DB、カバーの縮小画像)は `.env` の `OTOKURA_DATA_DIR`(既定は `./data`)に残るので、作り直しても再生回数などは消えません。

## 設定(環境変数)

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `OTOKURA_SOURCES` | `Lossless=/music/Lossless,1.Lossy=/music/1.Lossy` | 画面に出す名前=コンテナ内のパス。カンマ区切り |
| `OTOKURA_SCAN_AT` | `03:00` | 毎日の差分スキャン時刻。空で無効 |
| `OTOKURA_SCAN_ON_START` | `1` | 起動時にスキャンする |
| `OTOKURA_LRCLIB` | `1` | 歌詞が無い曲だけ LRCLIB から探す |
| `OTOKURA_DATA` | `/data` | データの置き場所(コンテナ内) |

## 対応形式

| 形式 | 今の対応 |
| --- | --- |
| FLAC / MP3 / AAC(M4A)/ Ogg Vorbis / Opus / WAV | ライブラリ表示とブラウザでの再生 |
| ALAC / AIFF / APE / TTA / WavPack / DSD / MPC | ライブラリ表示のみ(再生は次の段階で FLAC に変換して配信) |

## 開発

```sh
cd backend
pip install -r requirements.txt pytest
python -m pytest -q          # ffmpeg があればタグ・スキャン・API の通しテストも走る
OTOKURA_SOURCES="Music=/path/to/music" OTOKURA_DATA=./data uvicorn otokura.app:app --reload --port 8686
```

`web/index.html` は 1 ファイルの画面です。サーバーから開くとライブラリにつながり、ファイルを直接開くとデモデータで動きます。API の一覧は `http://<アドレス>:8686/api/docs`。

## 次の段階

1. APE / TTA / DSD などの変換配信(ffmpeg、変換キャッシュ)
2. Chromecast / Google スピーカー / DLNA・UPnP へのキャスト
3. アーティスト画像・アルバム情報の Web 取得と更新確認
4. ReplayGain、ギャップレス、スマートフェード
