"""ライブラリのスキャン(差分)。音楽ファイルには書き込まない(書き込みはタグ編集の tagedit.py だけ)。"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import art, chapters, cue, metadata
from .config import Config, Source
from .db import Database

log = logging.getLogger("otokura.scan")
SKIP_DIRS = ("#recycle", "@eadir", "$recycle.bin", "lost+found", ".@__thumb")
BATCH = 200
SCAN_REV = "2"     # 読むタグを増やしたら上げる。上がったら次のスキャンは全ファイルを読み直す(2: ReplayGain・MP4 のチャプター)


@dataclass
class ScanState:
    running: bool = False
    phase: str = "idle"           # idle / listing / reading / finishing
    done: int = 0
    total: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"running": self.running, "phase": self.phase, "done": self.done, "total": self.total,
                "startedAt": self.started_at, "finishedAt": self.finished_at, "added": self.added,
                "updated": self.updated, "removed": self.removed, "errors": self.errors[-20:],
                "errorCount": len(self.errors), "missingSources": self.missing_sources}


def _is_cd_dir(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"(?i)(cd|disc|disk)\s*\d+", name.strip()))


def album_folder(rel: Path) -> str:
    parts = rel.parts[:-1]
    if parts and _is_cd_dir(parts[-1]):
        parts = parts[:-1]
    return "/".join(parts)


def album_key(source: str, info: metadata.TrackInfo, rel: Path) -> str:
    folder = album_folder(rel)
    if info.album and info.albumartist:
        return f"{source}|aa|{info.albumartist.casefold()}|{info.album.casefold()}"
    if info.album:
        return f"{source}|f|{folder}|{info.album.casefold()}"
    return f"{source}|f|{folder}|"


class Scanner:
    def __init__(self, db: Database, cfg: Config, lyrics_store=None):
        self.db, self.cfg, self.lyrics = db, cfg, lyrics_store
        self.state = ScanState()
        self._lock = threading.Lock()
        self.on_done = None          # 完了時に呼ぶ(ライブラリの ETag 更新など)

    # ---------- 実行 ----------
    def start(self, full: bool = False) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run, args=(full,), daemon=True, name="scan").start()
        return True

    def run(self, full: bool = False) -> ScanState:
        """同じスレッドで最後まで実行する(テスト・CLI 用)。"""
        with self._lock:
            self._scan(full)
        return self.state

    def update_files(self, items: list[tuple[Source, str]], new_art: bool = False, wait_s: float = 10) -> bool:
        """タグを書き換えたファイルだけ読み直す。スキャン中で待っても空かなければ False。
        new_art: カバーを差し替えたので、アルバムの縮小画像を作り直す。"""
        if not self._lock.acquire(timeout=wait_s):
            return False
        try:
            self._pending = []
            c = self.db.conn
            touched: set[int] = set()
            for src, rels in items:
                p = src.path / rels
                old = self.db.one("SELECT id,source,rel,mtime,size,album_id FROM tracks WHERE source=? AND rel=?",
                                  (src.id, rels))
                info = metadata.read(p, Path(rels))
                if info is None:
                    continue
                with self.db.write_lock:
                    if new_art and old and old["album_id"]:
                        c.execute("UPDATE albums SET art=NULL, art_src=NULL WHERE id=?", (old["album_id"],))
                    aid = self._album(src, info, Path(rels), p)
                    if new_art:     # アルバム名が変わらず既に画像があっても、この曲の画像で作り直す
                        c.execute("UPDATE albums SET art=NULL WHERE id=?", (aid,))
                        self._art(aid, album_key(src.id, info, Path(rels)), info, p)
                    tid = self._upsert_track(src, rels, p.stat(), info, aid, old)
                touched.add(aid)
                if old and old["album_id"]:
                    touched.add(old["album_id"])
                self._lyrics(tid, src, rels, info)
            with self.db.write_lock:
                self._finish_albums(touched)
                c.commit()
            self._flush(c)
            if self.on_done:
                self.on_done()
            return True
        finally:
            self._lock.release()

    def _run(self, full: bool):
        try:
            self._scan(full)
        finally:
            self._lock.release()

    def _scan(self, full: bool):
        st = self.state = ScanState(running=True, phase="listing", started_at=time.time())
        self._pending = []
        try:
            full = full or self.db.meta("scan_rev") != SCAN_REV
            work: list[tuple[Source, Path, os.stat_result]] = []
            alive: list[Source] = []
            for src in self.cfg.sources:
                if not src.path.is_dir():
                    st.missing_sources.append(src.name)
                    log.warning("ソースが見つかりません: %s (%s)", src.name, src.path)
                    continue
                alive.append(src)
                for p in self._walk(src.path):
                    try:
                        work.append((src, p, p.stat()))
                    except OSError as e:
                        st.errors.append(f"{p}: {e}")
            st.total, st.phase = len(work), "reading"
            touched: set[int] = set()
            seen: dict[str, set[str]] = {s.id: set() for s in alive}
            existing = {(r["source"], r["rel"]): r for r in self.db.q("SELECT id,source,rel,mtime,size,album_id FROM tracks")}
            c = self.db.conn
            n = 0
            cues = cue.CueIndex()
            for src, p, stt in work:
                rel = p.relative_to(src.path)
                rels = rel.as_posix()
                hit = cues.find(p)
                if not hit and (src.id, rels) not in existing and not full:     # 前回、埋め込みの CUE で分けたファイル
                    first = existing.get((src.id, rels + "#01"))
                    if first and abs(first["mtime"] - stt.st_mtime) < 1e-3 and first["size"] == stt.st_size:
                        for (sid, r2), row in existing.items():
                            if sid == src.id and r2.startswith(rels + "#"):
                                seen[src.id].add(r2)
                                self._lyrics(row["id"], src, r2)
                        st.done += 1
                        continue
                if hit:     # CUE シートで 1 つのファイルが何曲にも分かれている
                    try:
                        n += self._scan_cue(src, p, rel, rels, stt, hit, existing, seen[src.id], touched, full)
                    except Exception as e:
                        st.errors.append(f"{src.name}/{rels}: {e}")
                    st.done += 1
                    if len(self._pending) >= BATCH:
                        self._flush(c)
                    continue
                seen[src.id].add(rels)
                old = existing.get((src.id, rels))
                if old and not full and abs(old["mtime"] - stt.st_mtime) < 1e-3 and old["size"] == stt.st_size:
                    st.done += 1
                    self._lyrics(old["id"], src, rels)
                    if len(self._pending) >= BATCH:
                        self._flush(c)
                    continue
                try:
                    info = metadata.read(p, rel)
                except Exception as e:   # 壊れたファイルなど
                    info = None
                    st.errors.append(f"{src.name}/{rels}: {e}")
                if info is None:
                    st.done += 1
                    continue
                emb = cue.embedded(info.cuesheet, p) or chapters.read(p)
                if emb:     # タグに埋め込まれた CUE シート(TAK・APE の 1 ファイルのアルバムに多い)か MP4 のチャプターで分ける
                    seen[src.id].discard(rels)
                    try:
                        n += self._scan_cue(src, p, rel, rels, stt, emb, existing, seen[src.id], touched, True, info)
                    except Exception as e:
                        st.errors.append(f"{src.name}/{rels}: {e}")
                    st.done += 1
                    continue
                with self.db.write_lock:
                    aid = self._album(src, info, rel, p)
                    tid = self._upsert_track(src, rels, stt, info, aid, old)
                touched.add(aid)
                if old:
                    st.updated += 1
                    if old["album_id"]:
                        touched.add(old["album_id"])
                else:
                    st.added += 1
                self._lyrics(tid, src, rels, info)
                st.done += 1
                n += 1
                if n % BATCH == 0 or len(self._pending) >= BATCH:
                    self._flush(c)
            st.phase = "finishing"
            with self.db.write_lock:
                for src in alive:     # 消えたファイルを外す(見えていたソースだけ)
                    gone = [r["id"] for (sid, rel), r in existing.items() if sid == src.id and rel not in seen[src.id]]
                    for i in range(0, len(gone), 500):
                        chunk = gone[i:i + 500]
                        for r in c.execute(f"SELECT DISTINCT album_id FROM tracks WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                            if r[0]:
                                touched.add(r[0])
                        c.execute(f"DELETE FROM tracks WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                    st.removed += len(gone)
                self._finish_albums(touched)
                c.commit()
            self._flush(c)
            self.db.meta("scan_rev", SCAN_REV)
        except Exception as e:
            log.exception("スキャンに失敗しました")
            st.errors.append(f"スキャンが途中で止まりました: {e}")
        finally:
            st.running, st.phase, st.finished_at = False, "idle", time.time()
            self.db.meta("last_scan", str(st.finished_at))
            if self.on_done:
                try:
                    self.on_done()
                except Exception:
                    log.exception("on_done")
            log.info("スキャン完了: 追加 %d / 更新 %d / 削除 %d / エラー %d", st.added, st.updated, st.removed, len(st.errors))

    def _scan_cue(self, src, p, rel, rels, stt, hit, existing, seen, touched, full, info=None) -> int:
        """CUE シートの曲を「元のファイル#曲番号」として登録する。CUE を書き換えたときも読み直す。"""
        sheet, tracks = hit
        try:
            mtime = max(stt.st_mtime, sheet.path.stat().st_mtime) if sheet.path != p else stt.st_mtime
        except OSError:
            mtime = stt.st_mtime
        size = stt.st_size
        vrels = [f"{rels}#{t.no:02d}" for t in tracks]
        seen.update(vrels)
        olds = [existing.get((src.id, v)) for v in vrels]
        if not full and all(o and abs(o["mtime"] - mtime) < 1e-3 and o["size"] == size for o in olds):
            for o, v in zip(olds, vrels):
                self._lyrics(o["id"], src, v)
            return 0
        info = info or metadata.read(p, rel)
        if info is None:
            return 0
        if not info.picture and p.suffix.lower() in chapters.EXT and not art.find_folder_image(p.parent):
            info = replace(info, picture=chapters.video_frame(p))   # カバーの無いライブ映像は映像の 1 コマを使う
        year = metadata._int(sheet.date) if sheet.date else None
        stamp = type("S", (), {"st_mtime": mtime, "st_size": size})()
        for t, v, old in zip(tracks, vrels, olds):
            end = t.end_ms if t.end_ms is not None else info.duration_ms
            ti = replace(info, title=t.title or f"Track {t.no:02d}", artist=t.performer or sheet.performer or info.artist,
                         album=sheet.title or info.album, albumartist=sheet.performer or info.albumartist,
                         year=year or info.year, genre=sheet.genre or info.genre, track_no=t.no,
                         disc_no=sheet.disc or info.disc_no, duration_ms=max(0, end - t.start_ms), guessed=False)
            with self.db.write_lock:
                aid = self._album(src, ti, rel, p)
                tid = self._upsert_track(src, v, stamp, ti, aid, old, (t.start_ms, t.end_ms))
            touched.add(aid)
            if old and old["album_id"]:
                touched.add(old["album_id"])
            if old:
                self.state.updated += 1
            else:
                self.state.added += 1
            self._lyrics(tid, src, v, ti)
        return len(tracks)

    # ---------- 部品 ----------
    def _walk(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@")) and d.lower() not in SKIP_DIRS)
            for f in sorted(filenames):
                if f.startswith("._"):
                    continue
                if os.path.splitext(f)[1].lower() in metadata.AUDIO_EXT:
                    yield Path(dirpath) / f

    def _album(self, src: Source, info: metadata.TrackInfo, rel: Path, path: Path) -> int:
        key = album_key(src.id, info, rel)
        c = self.db.conn
        r = c.execute("SELECT id, art FROM albums WHERE key=?", (key,)).fetchone()
        if r is None:
            title = info.album or (rel.parent.name if rel.parent.parts else "(フォルダなし)")
            cur = c.execute("INSERT INTO albums(key,title,artist,year,genre,source,folder,added_at) VALUES(?,?,?,?,?,?,?,?)",
                            (key, title, info.albumartist or info.artist, info.year, info.genre, src.id,
                             album_folder(rel), time.time()))
            aid, has_art = cur.lastrowid, False
        else:
            aid, has_art = r["id"], bool(r["art"])
        if not has_art:
            self._art(aid, key, info, path)
        return aid

    def _art(self, aid: int, key: str, info: metadata.TrackInfo, path: Path):
        data, src = info.picture, f"embedded:{path}"
        if not data:
            img = art.find_folder_image(path.parent)
            if img is None and _is_cd_dir(path.parent.name):
                img = art.find_folder_image(path.parent.parent)
            if img is None:
                return
            try:
                data, src = img.read_bytes(), f"file:{img}"
            except OSError:
                return
        res = art.make_thumbs(data, self.cfg.art_dir, key)
        if res:
            self.db.conn.execute("UPDATE albums SET art=?, art_src=?, hue=? WHERE id=?", (res[0], src, res[1], aid))

    def _upsert_track(self, src, rels, stt, info, aid, old, cue_range=(None, None)) -> int:
        c = self.db.conn
        vals = (stt.st_mtime, stt.st_size, aid, info.title, info.artist, info.albumartist, info.album, info.year,
                info.genre, info.track_no, info.disc_no, info.duration_ms, info.codec, info.sample_rate,
                info.bit_depth, info.bitrate, int(info.fixed), int(info.guessed), info.rg_track, info.rg_album, *cue_range)
        if old:
            c.execute("UPDATE tracks SET mtime=?,size=?,album_id=?,title=?,artist=?,albumartist=?,album=?,year=?,genre=?,"
                      "track_no=?,disc_no=?,duration_ms=?,codec=?,sample_rate=?,bit_depth=?,bitrate=?,tag_fixed=?,"
                      "tag_guessed=?,rg_track=?,rg_album=?,cue_start=?,cue_end=? WHERE id=?", (*vals, old["id"]))
            return old["id"]
        cur = c.execute("INSERT INTO tracks(mtime,size,album_id,title,artist,albumartist,album,year,genre,track_no,"
                        "disc_no,duration_ms,codec,sample_rate,bit_depth,bitrate,tag_fixed,tag_guessed,rg_track,rg_album,cue_start,cue_end,"
                        "source,rel,added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*vals, src.id, rels, time.time()))
        return cur.lastrowid

    def _finish_albums(self, ids: set[int]):
        """アルバムの代表情報(アーティスト・年・ジャンル)を曲から決め直し、空のアルバムを消す。"""
        c = self.db.conn
        for aid in ids:
            rows = c.execute("SELECT artist, albumartist, year, genre, rel FROM tracks WHERE album_id=?", (aid,)).fetchall()
            if not rows:
                c.execute("DELETE FROM albums WHERE id=?", (aid,))
                continue
            aa = Counter(r["albumartist"] for r in rows if r["albumartist"])
            arts = Counter(r["artist"] for r in rows)
            if aa:
                artist = aa.most_common(1)[0][0]
            elif len(arts) > 1 and arts.most_common(1)[0][1] < len(rows) * 0.6:
                artist = "Various Artists"
            else:
                artist = arts.most_common(1)[0][0]
            years = [r["year"] for r in rows if r["year"]]
            genres = Counter(r["genre"] for r in rows if r["genre"])
            c.execute("UPDATE albums SET artist=?, year=?, genre=?, folder=? WHERE id=?",
                      (artist, min(years) if years else None, genres.most_common(1)[0][0] if genres else None,
                       album_folder(Path(rows[0]["rel"])), aid))

    def _lyrics(self, tid: int, src: Source, rels: str, info: metadata.TrackInfo | None = None):
        """歌詞の更新は、ライブラリの書き込みを確定してからまとめて行う(同じ DB ファイルの書き込み待ちを避ける)。"""
        if self.lyrics is None:
            return
        self._pending.append((tid, src, rels, info))

    def _flush(self, c):
        with self.db.write_lock:
            c.commit()
        if self.lyrics is None:
            self._pending = []
            return
        from .lyrics.store import TrackRef
        for tid, src, rels, info in self._pending:
            try:
                if info is None:
                    r = self.db.one("SELECT title, artist, album, duration_ms FROM tracks WHERE id=?", (tid,))
                    if r is None:
                        continue
                    t = TrackRef(str(tid), str(src.path / rels), r["title"], r["artist"], r["album"], r["duration_ms"])
                else:
                    t = TrackRef(str(tid), str(src.path / rels), info.title, info.artist, info.album, info.duration_ms)
                self.lyrics.refresh(t)
            except Exception as e:
                self.state.errors.append(f"歌詞 {src.name}/{rels}: {e}")
        self._pending = []


class Scheduler(threading.Thread):
    """毎日決まった時刻に差分スキャンする。"""

    def __init__(self, scanner: Scanner, at: str):
        super().__init__(daemon=True, name="scan-scheduler")
        self.scanner = scanner
        h, m = (int(x) for x in at.split(":"))
        self.hm = (h, m)
        self.stop_event = threading.Event()

    def seconds_until_next(self, now: float | None = None) -> float:
        lt = time.localtime(now if now is not None else time.time())
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, self.hm[0], self.hm[1], 0, 0, 0, -1))
        base = now if now is not None else time.time()
        if target <= base:
            target += 86400
        return target - base

    def run(self):
        while not self.stop_event.wait(self.seconds_until_next()):
            self.scanner.start()
