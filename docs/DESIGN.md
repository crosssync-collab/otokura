# 音蔵 設計メモ

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

## アカウント(otokura.auth)

- 管理者(admin)と家族(member)。アカウントは音蔵の DB だけに置き、NAS のユーザーは増やさない。音楽ファイルに触るのはコンテナだけ
- `/api/` はすべてログインが要る(`Auth.guard` を全ルートの依存にしている)。例外は `/api/auth/*` と、下の署名付きの曲・カバー。起動(`from_env`)では必ず有効で、切る環境変数は無い
- 家族の書き込みは `MEMBER_WRITES` の許可リストだけ。新しい API は既定で管理者だけになる。NAS のパスは管理者にだけ返す
- パスワードは scrypt、10 文字以上。ログインは 1 年保持(Cookie は HttpOnly・SameSite=Lax、https では Secure、DB にはハッシュ)。ログイン画面に名前の一覧は出さない(ユーザー名を入力。メールアドレスを想定するが、メールは送らない)
- 遠方の家族の入口: Tailscale Funnel で 8443 を公開する(転送先は 8686 だけで、NAS のほかのポートは出ない)。家族は Tailscale を入れない。試行の制限は 名前+接続元 5 回、名前 20 回、接続元 20 回で、待ち時間は 30 秒〜15 分。パスワードの照合は同時に 2 つまで。`X-Forwarded-For` が付いた接続は家の中とみなさない
- 最初の管理者は `data/setup-code.txt` の確認コードで登録する(先に他の人が管理者になれないように)。登録するとファイルは消える
- 管理者のパスワード忘れ: NAS で `python -m otokura.auth reset-code` を実行すると `data/reset-code.txt` に確認コードができ(15 分有効、1 回限り)、ログイン画面で再設定できる。NAS に入れる人だけができる。前のログインは切れる。家族の分は管理者が画面で再設定する
- `web/manual.html` は家族向けのマニュアル(画像込みの 1 ファイル)。ログインなしで開ける。アプリの画面を変えたら説明とスクリーンショットも見直す
- 人ごと: `track_stats`、`playlists`、`user_settings`(テーマ、自分の EQ プリセットなど)。アカウントの無かった頃の記録は最初の管理者(id=1)に移し、移す前の DB を `otokura.pre-accounts.db` に残す。端末ごと: エフェクト、キュー。全員で共通: 曲、歌詞の既定、Web の情報、音量の測定結果
- 歌詞: 管理者の編集は全員の既定(`lyrics_user`)。家族のタイミング編集はその人だけの修正(`lyrics_personal`)で、優先順は 自分の修正 > 管理者の修正 > 元の歌詞。ブラウザではなく DB に置くので、端末やアドレスが変わっても同じ。貼り付けとファイルの読み込みは管理者だけ(画面)
- 歌詞の自動取得: 手元に歌詞が無い曲を誰かが初めて開くと、画面が LRCLIB に問い合わせ、結果を `lyrics_user` に保存して全員で共有する(家族でも同じ。`.lrc` ファイルは作らない)。見つからなかった曲は 30 日間、自動では問い合わせない
- Cast・DLNA の機器はログインできないので、曲ごと・24 時間の HMAC 署名を URL に付ける(`t=期限.署名`)。曲の配信とカバーの GET にだけ効く
- 最初のパスワード変更: 管理者が作った・再設定したパスワードは `must_change`。本人が変えるまで `guard` が `/api/auth/` 以外を 403 にする。本人が決めるパスワードはユーザー名と同じにできない
- 管理者のログイン: パスワードだけでは入れない(`needsSecond` の間は `guard` が 403)。指紋・PIN(パスキー、`otokura.passkey`、ライブラリは `webauthn`)を端末ごとに登録するか、予備の認証アプリ(TOTP、RFC 6238)を使う。家族はどちらも任意で、パスキーを登録してもパスワードで入れる
- パスキー: 指紋は端末の中で確かめ、DB には公開鍵だけを置く。ユーザー確認を必須にする。サイトの名前はアクセスされたアドレスから取るので、`https`(または `localhost`)の名前のアドレスでだけ使える。確認の問題は 1 回限り・5 分。スマホをなくしたら、管理者は NAS の確認コードで、家族は管理者の再設定で消える
- 認証アプリ: 30 秒、前後 1 つのずれを許す。同じコードは 2 回使えない(`totp_last`)。鍵は DB にそのまま置く(ハッシュにできない)。QR コードは `segno` がサーバーで作り、外部のスクリプトに鍵を渡さない。登録すると他の端末はログアウトする
- テレビのログイン: テレビは 8 文字のコード(5 分)と秘密の id を受け取り、id で待つ。ログイン済みのスマホがコードを入れて本人確認(指紋。指紋が無い家族はパスワード)すると、テレビにログインを渡す。コードも id も 1 回限り
- 家族の DLNA は家の中のアドレスからだけ(Tailscale や転送元が不明な接続は外とみなす)。外から押して家で鳴らないように

## ダウンロード

`GET /api/tracks/{id}/download?format=`。`original` は元のファイル(CUE・チャプターの曲は切り出し)。`flac / wav / mp3 / aac / opus` は変換し、タグとカバー(FLAC・MP3・AAC のみ)を入れる。変換したものは `/data/transcode` に残り、上限も再生用と共通。画面では、非可逆の曲に可逆の形式を、同じ形式への変換を出さない。

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
| `loudness` | ffmpeg で測った音量(LUFS)と頭・お尻の無音 |
| `artist_info` / `album_info` | Web から取った情報。fetched_at(調べた時刻)と updated_at(内容が変わった時刻) |

## API

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET/POST | `/api/auth/state`、`setup`、`login`、`logout`、`password` | ログイン |
| GET/POST/PUT/DELETE | `/api/users...` | アカウントの管理(管理者) |
| GET | `/api/tracks/{id}/download`、`cast-link` | ダウンロード、Cast の機器に渡す署名付きのアドレス |
| GET | `/api/library` | 画面が最初に読む一覧(アルバム・曲・統計)。ETag 付き |
| GET | `/api/tracks/{id}/stream` | 音声の配信(Range 対応)。鳴らせない形式・CUE の曲は変換して配る。`?direct=1` その端末がそのまま鳴らせる(Safari の ALAC 等)、`?cast=1` Google Cast 向け(48kHz まで) |
| POST | `/api/tracks/{id}/prepare` | 次の曲を先に変換しておく |
| GET/PUT | `/api/tracks/{id}/meta`、`/api/albums/{id}/meta`、`.../cover` | タグ・カバーの表示と編集。`POST /api/tagedits/{id}/revert` で元に戻す |
| GET | `/mix/{name}.jpg` | ミックス・ショートカットの写真 |
| GET | `/api/art/{album_id}?size=300` | カバー画像 |
| POST | `/api/tracks/{id}/loudness` | この曲の音量と無音(無ければ測る)。`/api/loudness`、`.../start`、`.../stop` でまとめて測る |
| GET | `/api/artists/info?name=`、`/api/albums/{id}/info` | Web の情報。`fetch=1` で未取得なら取りに行く。`POST /api/artists/refresh`、`/api/albums/{id}/info/refresh` で取り直す |
| GET | `/api/artistart/{image}?size=300` | アーティスト写真 |
| GET/POST | `/api/webinfo`、`.../start`、`.../check?days=30`、`.../stop` | Web の情報の状況、未取得をまとめて取得、更新の確認、中止 |
| GET/POST/DELETE | `/api/dlna/devices` | DLNA の機器の一覧(`search=1` で探し直す)・手で追加・削除 |
| POST/GET | `/api/dlna/{id}/load`、`play`、`pause`、`stop`、`seek`、`volume`、`status` | DLNA の機器の操作 |
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

## MP4 のチャプター(otokura.chapters)

`.mp4` / `.m4a` に音声と 2 つ以上のチャプターがあれば、ffprobe で読んで CUE シートと同じ形にし、`元のファイル#曲番号` の曲として登録する。配信も CUE の切り出しと同じ。アルバムのタグが無ければファイルのタイトル(無ければファイル名)を 1 枚のアルバムにする。カバーが無ければ映像の 1 コマを使う。`SCAN_REV` を上げると次のスキャンで全ファイルを読み直す(既存の曲にもタグの追加やチャプターを反映するため)。

## 音量とつなぎ(otokura.loudness + 画面)

- ReplayGain のタグ(Vorbis・APE・ID3 TXXX・MP4・Opus の R128)を `tracks.rg_track / rg_album` に持つ。タグが無い曲は ffmpeg の ebur128 で測り、-18 LUFS 基準の値にする。silencedetect で頭・お尻の無音も測り、`loudness` 表に残す(ファイルが変わったら測り直す)
- 測るのは 聴く曲と次の曲(`POST /api/tracks/{id}/loudness`)か、設定の「まとめて測る」(1 曲ずつ nice 19)
- 音量は `audio.volume` で下げる方向だけ。Web Audio は使わない(画面を消しても止まらないこと、自動再生の制限で無音にならないことを優先)。同じアルバムが続くときはアルバムの値
- つなぎは audio 要素 2 つ。曲の終わりの 25 秒前から次の曲を先読みし、`gap`(アルバムの続きの曲)は終わる約 10 ミリ秒前に入れ替え、`fade`(別のアルバムへ)はお尻の無音の手前から頭の無音を飛ばして重ねる。先読みが間に合わなければ曲の終わりでふつうに切り替える
- 要素の入れ替えなのでサンプル単位の正確さはない。iPhone は volume を変えられず、操作なしで別の要素を鳴らせないので、音量調整もつなぎも行わない

## エフェクト(画面の Web Audio)

- 経路: audio 要素 2 つ → 要素ごとの持ち上げ → イコライザー → 真空管 → エコー → 空間 → 夜間モード → リミッター → 出力。使う部品だけをつなぐ。すべてこの端末の処理で、サーバーは関わらない
- オンにして何かを選ぶまで AudioContext を作らず、audio 要素から直接鳴らす(画面を消したときの動きを変えないため)。一度つないだ audio 要素は元に戻せないので、その後すべて切ると素通しの経路にする。AudioContext はユーザーの操作の中で作り、再開する
- `audio.volume` はこの経路でも効く(実測)ので、ReplayGain の下げとフェードはそのまま。リミッターが動いているときだけ、要素ごとの持ち上げ(最大 +12dB)で静かな曲も上げる
- 空間の残響は計算で作り(録音は使わない)、計算する長さは 3 秒まで。広さは最初の遅れと返ってくる音で出す。秒数はホール以外は目安で、耳で調整する前提
- 設定は localStorage `otokura.fx`(端末ごと)。自分のイコライザーのプリセットだけ `settings.ui.eqUser` でサーバーに置く
- ギャップレス: エフェクトの経路では再生位置がまとまって進むので、その分(最大 60 ミリ秒)早く次の曲を始め、前の曲は最後まで鳴らす
- 重さの実測: backup の Knowledge にある通り、PC で全部入り 2.2%。Fire TV Stick での実測はまだ無い

## この端末の曲(画面の IndexedDB)

- NAS を通さず、その端末のブラウザだけで再生する。PC と Android(Chrome 132 以降)はフォルダのハンドルを保存してそのまま読み、読めない端末(iPhone など)は曲を IndexedDB へ取り込む
- 2 段階で登録する。まずファイル名とフォルダ名だけで `pending` の曲を作って一覧に出し(このまま聴ける)、タグ・長さ・カバーは裏で 3 曲ずつ読む。読み終えるまで曲の番号は変えず、最後に 1 回アルバムを並べ直す。途中で閉じても、次に開いたとき続きを読む
- 長さはファイルの必要な所だけ読む(FLAC の STREAMINFO、MP3 の Xing・VBRI かビットレート、MP4 の mvhd、WAV のヘッダー、Ogg の最後のページ)。読めないものだけ audio 要素で調べる
- カバーは 1 辺 1000px までに縮めて保存し、URL は画面に出すときに作る
- 端末の曲を作り直すときは NAS の一覧を取り直さない(`localRebuild`)
- 許可は保存したハンドルに `queryPermission` で聞く。求められるのは操作のあとだけなので、裏の読み取りでは求めず、許可が出るまで待つ

## 歌詞と並べるジャケット

標準の歌詞表示で、縦並びは歌詞の領域(.xl)の高さが 640px 以上のとき歌詞の上に、横並び(.wide)は幅 1200px 以上で横長(3:2 以上)のとき歌詞の左に出す。スマホの大きさでは出さない。全画面の歌詞と編集モードでも出さない。CSS だけで切り替える。

## ビジュアライザーとロード画面

- ビジュアライザーは自作の Canvas 5 種と Butterchurn(MIT、WebGL 2 が要る、jsDelivr から読む)。低性能の端末(4 コア以下・メモリ 2GB 以下・Silk)では 960px・約 30 コマ。4 秒ごとに実測し、18 コマ未満なら解像度を下げ、それでも遅いミルクドロップはリングに切り替える。Cast・DLNA の間は使えない。リモコンの ←→ で切り替え、戻るで閉じる
- ロード画面の動きは transform と opacity だけ(描画が JavaScript に止められないように)。ページの切り替えでは、端末ごとに描画時間を測り、140 ミリ秒を超えるページだけ先にロード画面を出してから描く

## Web の情報(otokura.webinfo)

- アーティスト: MusicBrainz で本人を特定(名前が一致し、手元のアルバムと重なる候補)→ Wikidata → Wikipedia の紹介文。写真は Deezer(名前が一致し、手元のアルバムと重なるときだけ)→ 無ければ Wikimedia Commons
- アルバム: MusicBrainz(タイトルとアーティストが一致)で発売日・種類・レーベル、Wikipedia の紹介文。無ければ iTunes の発売日
- 取るのは ページを開いたとき(設定で切れる)か、設定の「未取得をまとめて取得」。1 件ずつ、MusicBrainz は 1 秒 1 回まで
- 更新の確認: 取ってから 30 日経ったものを、毎日のスキャンのあとに 300 件まで調べ直す(設定の「更新を確認」ですぐにも)。内容が変わると `updated_at` を更新し、設定の「最近変わった情報」に出す。通信の失敗では今ある情報を消さない
- 結果は `artist_info` / `album_info` 表と `/data/art`。音楽ファイルには書き込まない

## DLNA・UPnP(otokura.dlna)

- 探す: SSDP の M-SEARCH をマルチキャストで送る。コンテナは host ネットワークで動かす(ブリッジではマルチキャストが外へ出ない。直接送っても、機器は毎回違うポートから返事をするので NAT に捨てられ、foobar2000 Renderer のように直接送った分には答えない機器もある)。IP アドレスを入れると、返事の中からそのアドレスの機器を選んで残す(`meta.dlna_manual`)
- 操作: AVTransport(SetAVTransportURI・Play・Pause・Stop・Seek・GetPositionInfo・GetTransportInfo)と RenderingControl(音量)の SOAP を NAS から送る。機器は `OTOKURA_CAST_URL` の `/api/tracks/{id}/stream?cast=1&dlna=1` を取りに来る(HEAD・Range・DLNA の応答ヘッダーに対応。48kHz を超える曲は Cast と同じく変換)
- 画面は 1.5 秒ごとに状態を読む。止まったとき、最後に見た位置が曲の終わりの 6 秒以内なら次の曲へ、それ以外は機器側で止めたものとして止まる
- キューは画面が持つ(Cast と同じ)ので、次の曲へ進むのは画面を開いている間
- 家の中のアドレス以外には接続しない。説明ファイルの controlURL が別のホストを指す機器も使わない
