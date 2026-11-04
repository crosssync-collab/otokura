# 音蔵 バックエンド — 歌詞モジュール

歌詞の読み込み・保存・API。本体(ライブラリスキャナ / FastAPI アプリ)ができたらそこへ組み込む。

## 構成

| ファイル | 役割 |
| --- | --- |
| `otokura/lyrics/model.py` | 内部形式(行・語・歌い手・バックボーカル・レイヤー)。OpenSubsonic songLyrics v2 に寄せた形 |
| `otokura/lyrics/lrc.py` | LRC / Enhanced LRC(語の時刻)/ `v1:` `v2:` 歌い手 / `[bg:]` / `[offset:]` / 同時刻 2 行目を訳として分離 / 空白行は段落の区切り(待機の行)として残す |
| `otokura/lyrics/ttml.py` | TTML(Apple 形式の歌い手・x-bg・訳/読みを含む) |
| `otokura/lyrics/tags.py` | 埋め込みタグ(ID3 SYLT/USLT、Vorbis LYRICS、MP4 ©lyr、APEv2) |
| `otokura/lyrics/encoding.py` | ファイルの文字コード判定、CP932 文字化けの修復 |
| `otokura/lyrics/resolver.py` | 同名サイドカーとタグから最良の歌詞を選ぶ(同期あり > 語の時刻あり > .ttml > .lrc > タグ > .txt) |
| `otokura/lyrics/store.py` | SQLite 保存。手動 > 手元 > 歌詞フォルダ > LRCLIB の順で表示。ずれ補正。ファイル更新時刻で差分更新。手動の歌詞は 1 つ前の版を残す |
| `otokura/lyrics/folder.py` | 歌詞フォルダ(`アーティスト - 曲名.lrc` など。区切りが違う名前も、つなげた名前で合わせる。同じ種類なら新しいファイル) |
| `otokura/lyrics/lrclib.py` | LRCLIB 取得(長さが 2 秒以上違う候補は捨てる。見つからない曲は 30 日待つ) |
| `otokura/lyrics/api.py` | FastAPI ルーター |

## 組み込み方

```python
from fastapi import FastAPI
from otokura.lyrics.api import make_router
from otokura.lyrics.lrclib import LrcLib
from otokura.lyrics.store import LyricsStore, TrackRef

store = LyricsStore("/data/otokura.db", lrclib=LrcLib())   # 外部取得をしないなら lrclib=None

def get_track(track_id: str) -> TrackRef | None:
    ...  # ライブラリ DB から path / title / artist / album / duration_ms を引く

app = FastAPI()
app.include_router(make_router(store, get_track), prefix="/api")
```

スキャナからは曲ごとに `store.refresh(TrackRef(...))` を呼ぶ(変化がなければ何もしない)。
夜間の一括取得は `store.missing()` の曲に `store.fetch_lrclib()`。

## API

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/api/tracks/{id}/lyrics` | 表示用の歌詞と `offsetMs` |
| PUT | `/api/tracks/{id}/lyrics/offset` | `{"offsetMs": 300}`(＋で歌詞を早く出す、±10 秒まで) |
| POST | `/api/tracks/{id}/lyrics/fetch` | LRCLIB から取得 |
| PUT | `/api/tracks/{id}/lyrics` | `{"text": "..."}` 貼り付け(LRC / TTML / テキスト自動判定) |
| POST | `/api/tracks/{id}/lyrics/upload` | ファイル(CP932 も可) |
| DELETE | `/api/tracks/{id}/lyrics/manual` | 手動登録を削除 |

ダッシュボードは `window.OTOKURA_API = ''` を設定すると、この API を使う(未設定ならモック)。

## テスト

```
pip install mutagen fastapi httpx python-multipart pytest
python -m pytest -q        # ffmpeg があればタグ・API のテストも走る
```

LRCLIB の公式なレート制限値は未確認。クライアントは 1 秒 1 回に抑え、429 が返れば Retry-After を守って止まる。
