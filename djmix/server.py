"""FastAPI アプリ。画面(frontend/)の配信と API を提供する。"""
from __future__ import annotations

import asyncio
import threading
import time
import json
import logging
import mimetypes
import re
import os
from contextlib import asynccontextmanager

import numpy as np

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db, edits, mixes, prefs, queues, settings
from .analysis import cache, cutpoints, sections
from .analysis.bulk import BulkAnalyzer
from .analysis.worker import PRIO_MANUAL, AnalysisWorker
from .planner import desc as planner_desc
from .planner import genres as planner_genres
from .audio.pcm import SAMPLE_RATE, PcmStore
from .config import FRONTEND_DIR, MAPPINGS_DIR
from .midi.bridge import MidiBridge
from .library import query
from .library.scanner import Scanner

log = logging.getLogger("djmix")
from .library import roots as lib_roots  # noqa: E402
from .library import itunes  # noqa: E402
scanner = Scanner(lib_roots.configured() or None)
worker = AnalysisWorker()
bulk = BulkAnalyzer(worker)
pcm_store = PcmStore()
midi = MidiBridge()

# Windows ではレジストリの設定次第で .js が text/plain になり、ES Module の読み込みが拒否される。明示的に上書きする
for _ext, _type in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                    (".wasm", "application/wasm"), (".json", "application/json"), (".svg", "image/svg+xml")):
    mimetypes.add_type(_type, _ext)

@asynccontextmanager
async def lifespan(_app):
    # 解析アルゴリズムを更新した（ANALYSIS_VERSION が変わった）曲は未解析に戻す。使うときに自動で解析し直す
    with db.tx() as c:
        c.execute("UPDATE tracks SET analysis_status = 'none' WHERE analysis_status = 'done' AND "
                  "(analysis_version IS NULL OR analysis_version != ?)", (cache.ANALYSIS_VERSION,))
        # 前回の終了時に解析待ち・解析中だった曲も未解析に戻す（キューは画面側が依頼し直す）
        c.execute("UPDATE tracks SET analysis_status = 'none' WHERE analysis_status IN ('queued', 'running')")
    # 音楽フォルダを複数にする前の曲に、どの音楽フォルダの曲かを記録する（ドライブが外れていても隠れないように。I-3）
    lib_roots.backfill()
    # 子プロセスが落ちたせいで「解析失敗」になった曲を未解析に戻す（2026-10-01 の一晩の解析）
    bulk.recover()
    # iTunes の情報: 起動時と 5 分ごとに XML の更新を見て読み直す（Phase 5 E）
    if os.environ.get("DJMIX_NO_ITUNES") != "1":
        itunes.start_watch()
    # 起動のたびに差分走査する（変わっていない曲はタグを読み直さないので2回目以降は速い）
    if os.environ.get("DJMIX_NO_SCAN") != "1":
        scanner.start()
    if os.environ.get("DJMIX_NO_MIDI") != "1":
        midi.start(asyncio.get_running_loop())
    # おまかせ用の要約が無い解析済みの曲（Phase 3 より前に解析した曲など）の要約を作る
    threading.Thread(target=planner_desc.backfill, name="desc-backfill", daemon=True).start()
    yield
    bulk.shutdown()      # まとめて解析の子プロセスを止める（解析中の曲は次回、未解析から）


app = FastAPI(title="DJ Mixer", version=__version__, lifespan=lifespan)

# ---- 自分の画面からの要求だけを受け付ける（2026-11-01。公開にあたっての見直し） ----
# このサーバーは 127.0.0.1 でだけ待ち受けるが、同じ PC のブラウザで開いた別のサイトから、
# ファイルの書き出し・エクスプローラーの起動・WebSocket（録音の保存）を要求されるおそれがある（CSRF・WebSocket の乗っ取り）。
# Host がこの PC の名前でないもの（DNS rebinding）と、Origin が自分の画面でないものを断る。
# Origin の無い要求（ブラウザ以外のツール・同じページの GET）は通す。
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}       # testserver は pytest の TestClient


def _host_only(v: str) -> str:
    v = v.strip().lower()
    if v.startswith("["):
        return v[: v.index("]") + 1] if "]" in v else v
    return v.split(":")[0]


class LocalOnlyMiddleware:
    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            h = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host = _host_only(h.get("host", ""))
            origin = h.get("origin")
            bad = host not in LOCAL_HOSTS
            if not bad and origin is not None:
                # Origin は、要求の宛先（Host。この PC の名前に限っている）とポートまで完全に同じでなければ断る
                # （同じ PC の別のポートで動いている別のアプリの画面からの要求も断る）
                bad = origin.strip().lower().rstrip("/") != f"http://{h.get('host', '').strip().lower()}"
            if bad:
                log.warning("rejected a request from outside the app: host=%s origin=%s path=%s", h.get("host"), origin, scope.get("path"))
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": "forbidden".encode()})
                return
        await self.app(scope, receive, send)


app.add_middleware(LocalOnlyMiddleware)


@app.middleware("http")
async def no_cache(request, call_next):
    # ローカルアプリなので画面ファイルはキャッシュさせない。
    # 更新後に古い JS（や誤った Content-Type のまま保存された応答）が使われ続けるのを防ぐ
    is_static = not request.url.path.startswith("/api/")
    if is_static:
        # 条件付きリクエストに 304 を返すと、ブラウザは保存済みの古いヘッダーごと再利用するので、常に本体を返す
        request.scope["headers"] = [(k, v) for k, v in request.scope["headers"]
                                    if k not in (b"if-none-match", b"if-modified-since")]
    resp = await call_next(request)
    if is_static:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


class Diagnostics(BaseModel):
    """画面側の実行環境チェック結果（WebView2 で AudioWorklet 等が使えるか）。"""

    userAgent: str
    checks: dict[str, bool | str]


@app.post("/api/diagnostics")
def diagnostics(d: Diagnostics) -> dict:
    ok = all(v is True for v in d.checks.values() if isinstance(v, bool))
    log.info("frontend diagnostics %s: %s | %s", "OK" if ok else "NG", d.checks, d.userAgent)
    return {"ok": ok}


# ---- ライブラリ（P1-2） ----


@app.get("/api/library")
def library(q: str = "", folder: str | None = None, analyzed: bool | None = None,
            sort: str = "path", offset: int = 0, limit: int = Query(200, le=1000),
            desc: bool = False, root: str | None = None, artist: str | None = None, album: str | None = None,
            playlist: int | None = None, artist_q: str = "", album_q: str = "", bpm_min: float | None = None,
            bpm_max: float | None = None, keys: str = "", groups: str = "", rating_min: int | None = None) -> dict:
    return query.search(q, folder, analyzed, sort, offset, limit, desc=desc, root=root, artist=artist, album=album, playlist=playlist,
                        artist_q=artist_q, album_q=album_q, bpm_min=bpm_min, bpm_max=bpm_max,
                        keys=[k for k in keys.split(",") if k], groups=[g for g in groups.split(",") if g], rating_min=rating_min)


# ---- iTunes の情報の取り込み（Phase 5 E。design_phase5.md §E） ----
@app.get("/api/itunes")
def itunes_status() -> dict:
    return itunes.status()


@app.post("/api/itunes/import")
async def itunes_import() -> dict:
    settings.save({"itunes_enabled": True})
    await run_in_threadpool(itunes.import_xml)
    return itunes.status()


@app.post("/api/itunes/unlink")
def itunes_unlink() -> dict:
    itunes.unlink()
    return itunes.status()


@app.get("/api/itunes/playlists")
def itunes_playlists() -> dict:
    return {"items": itunes.playlists()}


@app.get("/api/library/facets")
def library_facets(kind: str = "artist", artist: str | None = None) -> dict:
    if kind not in ("artist", "album"):
        raise HTTPException(400, "kind は artist か album")
    return {"items": query.facets(kind, artist)}


@app.get("/api/library/folders")
def library_folders() -> dict:
    return {"counts": query.counts(), "folders": query.folders(), "roots": lib_roots.configured()}


@app.get("/api/library/scan")
def scan_status() -> dict:
    return {**scanner.status(), "root": str(scanner.root) if scanner.root else "", "roots": [str(r) for r in scanner.roots]}


@app.post("/api/library/rescan")
def rescan() -> dict:
    return {"started": scanner.start()}


# ---- ミックスキュー（P1-2） ----
class QueueItem(BaseModel):
    track_id: int
    use_start: float | None = None
    use_end: float | None = None
    use_label: str | None = None
    join_next: int | None = None
    join_bars: int | None = None
    join_tpl: str | None = None
    join_params: dict | None = None


class QueueBody(BaseModel):
    items: list[QueueItem]


@app.get("/api/queue")
def get_queue(name: str = queues.CURRENT) -> dict:
    return {"name": name, "items": queues.load(name)}


@app.put("/api/queue")
def put_queue(body: QueueBody, name: str = queues.CURRENT) -> dict:
    if not name.strip():
        raise HTTPException(400, "name is empty")
    queues.save([i.model_dump() for i in body.items], name)
    return {"ok": True, "count": len(body.items)}


# ---- ミックス・好み・履歴（Phase 4 P4-1。design_phase4.md §1） ----
@app.get("/api/mixes")
def mixes_list(sort: str = "new") -> dict:
    return {"mixes": mixes.list_mixes(sort), "current": settings.load().get("current_mix")}


@app.get("/api/mixes/{mix_id}")
def mixes_get(mix_id: int) -> dict:
    m = mixes.get(mix_id)
    if m is None:
        raise HTTPException(404, "ミックスがありません")
    return m


@app.post("/api/mixes")
def mixes_create(body: dict = Body(default={})) -> dict:
    """{name, kind}: 今のキューの中身を新しいミックスとして残し、キューをそのミックスにつなぐ。"""
    m = mixes.save(queues.load(), str(body.get("name") or time.strftime("%Y-%m-%d %H%M ミックス")), body.get("kind", "hand"))
    settings.save({"current_mix": m["id"]})
    return m


@app.patch("/api/mixes/{mix_id}")
def mixes_rename(mix_id: int, body: dict = Body(default={})) -> dict:
    m = mixes.rename(mix_id, str(body.get("name", "")))
    if m is None:
        raise HTTPException(404, "ミックスがありません")
    return m


@app.post("/api/mixes/{mix_id}/duplicate")
def mixes_duplicate(mix_id: int) -> dict:
    m = mixes.duplicate(mix_id)
    if m is None:
        raise HTTPException(404, "ミックスがありません")
    return m


@app.delete("/api/mixes/{mix_id}")
def mixes_delete(mix_id: int) -> dict:
    ok = mixes.delete(mix_id)
    if settings.load().get("current_mix") == mix_id:
        settings.save({"current_mix": None})
    return {"ok": ok}


@app.post("/api/mixes/{mix_id}/open")
def mixes_open(mix_id: int, body: dict = Body(default={})) -> dict:
    """{mode: replace|append}: ミックスをキューに開く。置き換えたときはキューをそのミックスにつなぐ。"""
    m = mixes.get(mix_id)
    if m is None:
        raise HTTPException(404, "ミックスがありません")
    cur = queues.load() if body.get("mode") == "append" else []
    queues.save(cur + m["items"])
    settings.save({"current_mix": mix_id if body.get("mode") != "append" else None})
    return {"name": queues.CURRENT, "items": queues.load(), "mix": mix_id}


@app.post("/api/mixes/played")
def mixes_played() -> dict:
    """「ミックスを通して再生」を始めたとき: キューがミックスのままなら、そのミックスの再生回数を増やす。"""
    mid = settings.load().get("current_mix")
    if mid and mixes.is_current(mid):
        mixes.record_play(mid)
        return {"mix": mid}
    return {"mix": None}


@app.post("/api/mixes/after-export")
def mixes_after_export(body: dict = Body(default={})) -> dict:
    m = mixes.record_export(body, settings.load().get("current_mix"))
    settings.save({"current_mix": m["id"]})
    return m


@app.post("/api/tracks/{track_id}/rating")
def track_rating(track_id: int, body: dict = Body(default={})) -> dict:
    return {"track_id": track_id, "rating": prefs.set_rating(track_id, int(body.get("rating", 0)))}


@app.post("/api/plays")
def plays_add(body: dict = Body(default={})) -> dict:
    src = body.get("source") if body.get("source") in prefs.SOURCES else "deck"
    return {"recorded": prefs.record_plays([int(x) for x in body.get("track_ids", [])], src)}


@app.get("/api/plays")
def plays_list(limit: int = 50) -> dict:
    return {"items": prefs.history(max(1, min(500, limit)))}


@app.get("/api/queues")
def list_queues() -> dict:
    return {"queues": queues.names()}


# ---- 解析（P1-3） ----
@app.post("/api/tracks/{track_id}/analyze")
def analyze(track_id: int, priority: int = PRIO_MANUAL, force: bool = False) -> dict:
    if not query.get_tracks([track_id]):
        raise HTTPException(404, "track not found")
    return {"status": worker.request(track_id, priority, force)}


@app.get("/api/tracks")
def tracks(ids: str) -> dict:
    """ids=1,2,3 の曲情報（解析状態の更新を画面に反映する用）。"""
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:500]
    return {"items": list(query.get_tracks(id_list).values())}


@app.get("/api/tracks/{track_id}/analysis")
def track_analysis(track_id: int) -> dict:
    a = cache.read(track_id)
    if a is None:
        raise HTTPException(404, "not analyzed")
    return edits.apply(track_id, a)


class EditsPatch(BaseModel):
    meter: int | None = None
    bpm_scale: float | None = None
    grid_offset_ms: float | None = None
    add_anchor: int | None = None
    clear_anchors: bool | None = None
    hotcues: list[float | None] | None = None
    cue_point: float | None = None


@app.patch("/api/tracks/{track_id}/edits")
def patch_edits(track_id: int, body: EditsPatch) -> dict:
    """ユーザー修正を保存し、反映後の解析結果を返す。送られたキーだけ変更する（meter は null で自動に戻す）。"""
    if not query.get_tracks([track_id]):
        raise HTTPException(404, "track not found")
    edits.update(track_id, body.model_dump(exclude_unset=True))
    a = cache.read(track_id)
    return edits.apply(track_id, a) if a is not None else {"edits": edits.get(track_id)}


@app.get("/api/tracks/{track_id}/edits")
def get_edits(track_id: int) -> dict:
    return edits.get(track_id)


@app.get("/api/tracks/{track_id}/sections")
def track_sections(track_id: int) -> dict:
    """区間の候補（曲の構造の区切り）。design_phase2.md §2.3。"""
    segs = sections.for_track(track_id)
    if segs is None:
        raise HTTPException(404, "not analyzed")
    return {"sections": segs}


@app.get("/api/tracks/{track_id}/cutpoints")
def track_cutpoints(track_id: int) -> dict:
    """カットインの切り替えポイントの候補と、小節ごとの音量（design_cutin.md）。"""
    r = cutpoints.for_track(track_id)
    if r is None:
        raise HTTPException(404, "not analyzed")
    return r


@app.get("/api/tracks/{track_id}/wave")
def track_wave(track_id: int) -> Response:
    b = cache.wave_bytes(track_id)
    if b is None:
        raise HTTPException(404, "not analyzed")
    return Response(b, media_type="application/octet-stream")


@app.get("/api/tracks/{track_id}/pcm")
def track_pcm(track_id: int, offset: int = 0, frames: int = Query(441000, le=44100 * 60)) -> Response:
    """再生用 PCM。planar float32（L の frames 個→R の frames 個）、44.1kHz。総サンプル数は X-Total-Frames。"""
    row = db.connect().execute("SELECT path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "track not found")
    try:
        body, total = pcm_store.chunk(track_id, row["path"], offset, frames)
    except RuntimeError as e:
        raise HTTPException(422, str(e)) from e
    return Response(body, media_type="application/octet-stream",
                    headers={"X-Total-Frames": str(total), "X-Sample-Rate": str(SAMPLE_RATE),
                             "Access-Control-Expose-Headers": "X-Total-Frames, X-Sample-Rate"})


@app.post("/api/dev/sync-check")
async def dev_sync_check(request: Request, k: float = 1.0, windows: str | None = None) -> dict:
    """同期 E2E 試験: 左=A・右=B の録音（webm）を受け取り、拍のずれを返す（djmix/devtools.py）。
    windows="20.5-55,83-99.5" のように、比べる区間（録音の秒）を指定できる。"""
    from .devtools import sync_check

    data = await request.body()
    win = [tuple(float(v) for v in w.split("-")) for w in windows.split(",")] if windows else None
    return await run_in_threadpool(sync_check, data, k, 3.0, win)


@app.get("/api/tracks/{track_id}/stems")
def track_stems(track_id: int) -> dict:
    """小節ごとのパートの量（Phase 4 B1）。まだなら裏で求め始めて status='pending' を返す（画面は少し待って聞き直す）。"""
    from .analysis import stems as stems_mod
    from .analysis.worker import request_stems
    arr = stems_mod.read(track_id)
    if arr is not None:
        return {"status": "done", "sources": stems_mod.SOURCES, "bars": np.round(arr, 3).tolist(), "vocal_on": stems_mod.VOCAL_ON}
    r = db.connect().execute("SELECT path, analysis_status FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if r is None or r["analysis_status"] != "done":
        raise HTTPException(404, "解析されていません")
    request_stems(track_id, r["path"])
    return {"status": "pending"}


@app.get("/api/tracks/{track_id}/stems-audio")
def track_stems_audio(track_id: int, request: int = 1) -> dict:
    """分離した音（Phase 5 B2）の状態。まだなら（request=1 のとき）裏で分離を始めて status='pending'。"""
    from .analysis import stems as stems_mod
    from .analysis.worker import request_stems
    if stems_mod.audio_ready(track_id):
        return {"status": "done"}
    r = db.connect().execute("SELECT path, analysis_status FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if r is None or r["analysis_status"] != "done":
        raise HTTPException(404, "解析されていません")
    if not request:
        return {"status": "none"}
    request_stems(track_id, r["path"], audio=True)
    return {"status": "pending"}


@app.get("/api/tracks/{track_id}/stems-pcm")
def track_stems_pcm(track_id: int, offset: int = 0, frames: int = Query(441000, le=44100 * 120)) -> Response:
    """分離した音の一部。planar float32 × 8ch（ボーカル L・R、ドラム L・R、ベース L・R、その他 L・R の順に frames 個ずつ）。"""
    from .analysis import stems as stems_mod
    if not stems_mod.audio_ready(track_id):
        raise HTTPException(404, "分離した音がまだありません")
    y, total = stems_mod.read_audio(track_id, offset, frames)
    return Response(y.astype("<f4").tobytes(), media_type="application/octet-stream",
                    headers={"X-Total-Frames": str(total), "X-Frames": str(y.shape[1]), "X-Sample-Rate": str(SAMPLE_RATE),
                             "Access-Control-Expose-Headers": "X-Total-Frames, X-Frames, X-Sample-Rate"})


@app.get("/api/analysis/status")
def analysis_status() -> dict:
    return worker.status()


# ---- まとめて解析（Phase 3 P3-3。design_phase3.md §3.3） ----
@app.get("/api/bulk")
def bulk_status() -> dict:
    return bulk.status()


@app.post("/api/bulk/start")
def bulk_start(body: dict = Body(default={})) -> dict:
    groups = [g for g in body.get("groups") or [] if g in planner_genres.NAMES]
    bulk.start(groups, bool(body.get("rest", True)), bool(body.get("stems", False)))
    return bulk.status()


@app.get("/api/bulk/failed")
def bulk_failed() -> dict:
    return {"items": bulk.failed_list()}


@app.post("/api/bulk/retry-failed")
def bulk_retry_failed() -> dict:
    """解析できなかった曲を未解析に戻す。まとめて解析が止まっていれば、続けて始める。"""
    n = bulk.retry_failed()
    if n and not bulk.status()["running"]:
        bulk.start([], True, False)
    return {"reset": n, **bulk.status()}


@app.post("/api/bulk/pause")
def bulk_pause() -> dict:
    bulk.pause()
    return bulk.status()


@app.post("/api/bulk/playing")
def bulk_playing(body: dict = Body(default={})) -> dict:
    """画面から: どちらかのデッキが再生中か（再生中はまとめて解析を 1 曲ずつに減らす）。"""
    bulk.set_playing(bool(body.get("playing")))
    return {"ok": True}


# ---- おまかせ（Phase 3 P3-4。design_phase3.md §4） ----
@app.post("/api/auto/plan")
async def auto_plan(body: dict = Body(default={})) -> dict:
    """mode=library: {seed, groups, minutes, flow, avoid} → 3 案。mode=queue: {ids} → [おまかせの並び, 今の並び]。"""
    from .planner import engine
    try:
        jm = body.get("join_mode") if body.get("join_mode") in engine.JOIN_MODES else "auto"     # つなぎ方（design_cutin.md）
        if body.get("mode") == "queue":
            return await run_in_threadpool(engine.plan_queue, [int(x) for x in body.get("ids", [])], None, jm)
        if body.get("mode") == "albums":          # アルバムで選ぶ（2026-10-25）
            curve = [float(v) for v in body.get("curve") or []][:200]
            flow = "custom" if body.get("flow") == "custom" and len(curve) >= 2 else body.get("flow") if body.get("flow") in engine.FLOWS else "up"
            tl = body.get("track_len")
            pos = body.get("track_pos") if body.get("track_pos") in ("auto", "intro", "head", "peak", "tail", "full") else "auto"
            return await run_in_threadpool(engine.plan_albums, [str(a) for a in body.get("albums") or []],
                                           max(5.0, min(600.0, float(body.get("minutes", 40)))), flow,
                                           int(body["seed"]) if body.get("seed") else None, bool(body.get("use_all")),
                                           [int(x) for x in body.get("avoid", [])], None, curve, settings.load(),
                                           float(tl) if tl else None, pos, jm)
        curve = [float(v) for v in body.get("curve") or []][:200]
        flow = "custom" if body.get("flow") == "custom" and len(curve) >= 2 else body.get("flow") if body.get("flow") in engine.FLOWS else "up"
        tl = body.get("track_len")
        pos = body.get("track_pos") if body.get("track_pos") in ("auto", "intro", "head", "peak", "tail", "full") else "auto"
        return await run_in_threadpool(engine.plan_library, int(body["seed"]), list(body.get("groups") or []),
                                       max(5.0, min(600.0, float(body.get("minutes", 40)))), flow, [int(x) for x in body.get("avoid", [])],
                                       None, curve, settings.load(), [int(x) for x in body.get("playlists") or []],
                                       float(tl) if tl else None, pos, jm)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/auto/next")
async def auto_next(prev: int | None = None, next: int | None = None, groups: str = "", exclude: str = "", n: int = 8,
                    artist: list[str] = Query(default=[])) -> dict:
    """次の曲の候補（案Y）。prev の次に合う曲、next もあれば差し替え（前後の相性の平均）。
    artist（いくつでも）で、そのアーティストの曲だけに絞る。"""
    from .planner import engine
    gs = [g for g in groups.split(",") if g]
    ex = [int(x) for x in exclude.split(",") if x.strip().isdigit()]
    items = await run_in_threadpool(engine.next_candidates, prev, next, gs, ex, max(1, min(20, n)), None, settings.load(), artist)
    return {"items": items}


@app.get("/api/auto/albums")
def auto_albums(q: str = "") -> dict:
    """おまかせの「アルバムで選ぶ」のアルバムの一覧（2026-10-25）。"""
    return {"items": query.album_list(q.strip())}


@app.get("/api/auto/album-tracks")
def auto_album_tracks(album: list[str] = Query(default=[])) -> dict:
    """選んだアルバムの解析済みの曲（起点の曲を選ぶ）。"""
    return {"items": query.album_tracks(album)}


@app.post("/api/auto/rejoin")
async def auto_rejoin(body: dict = Body(default={})) -> dict:
    """結果の曲ごとの区間を変えて、同じ並びで繋ぎを計算し直す。{tracks: [{id, len, pos}]}（I-7）。"""
    from .planner import engine
    spec = [{"id": int(t["id"]), "len": float(t["len"]) if t.get("len") else None,
             "pos": t.get("pos") if t.get("pos") in ("auto", "intro", "head", "peak", "tail", "full", "custom") else "auto",
             "t0": float(t["t0"]) if t.get("t0") is not None else None, "t1": float(t["t1"]) if t.get("t1") is not None else None,
             "jm": t.get("jm") if t.get("jm") in ("cut", "mix") else None}       # その曲から次の曲へのつなぎ方（カットイン／ミックス）
            for t in body.get("tracks") or []]
    jm = body.get("join_mode") if body.get("join_mode") in engine.JOIN_MODES else "auto"
    try:
        return await run_in_threadpool(engine.rejoin, spec, None, settings.load(), jm)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/auto/segments")
async def auto_segments(id: int, len: float | None = None) -> dict:
    """1 曲の区間の候補（おまかせ・頭から・盛り上がり中心・終わりまで・曲全体）。len は 1 曲あたりの長さ（秒）。"""
    from .planner import engine
    return {"items": await run_in_threadpool(engine.segment_choices, id, len or None)}


@app.get("/api/auto/facets")
async def auto_facets() -> dict:
    """次の曲の候補の絞り込みの選択肢（解析済みの曲のアーティスト・ジャンルのまとまりと曲数）。"""
    from .planner import engine
    return await run_in_threadpool(engine.candidate_facets)


@app.get("/api/genre-groups")
def genre_groups() -> dict:
    """ジャンルのまとまりごとの曲数（全体・解析済み・未解析）。まとめて解析とおまかせの範囲の選択に使う。"""
    from .analysis.bulk import MAX_SEC, MIN_SEC
    counts = {n: {"name": n, "total": 0, "analyzed": 0, "left": 0} for n in planner_genres.NAMES}
    rw, rargs = lib_roots.where("")
    for r in db.connect().execute(f"SELECT genre, analysis_status FROM tracks WHERE duration BETWEEN {MIN_SEC} AND {MAX_SEC} AND {rw}", rargs):
        g = counts[planner_genres.group_of(r["genre"])]
        g["total"] += 1
        g["analyzed"] += r["analysis_status"] == "done"
        g["left"] += r["analysis_status"] == "none"
    return {"groups": list(counts.values())}


# ---- 書き出し: 再生しながら録音する（Phase 3 P3-7。design_phase3.md §6.1） ----
@app.websocket("/ws/record")
async def ws_record(ws: WebSocket) -> None:
    """画面から: 最初に {folder, name, format, sampleRate} → 16bit ステレオの PCM（バイナリ）を続けて →
    最後に {end: true, tracklist: [...]}。応答: {ok, path, tracklist, seconds} または {error}。"""
    from .export import writer
    await ws.accept()
    sink = None
    try:
        head = await ws.receive_json()
        fmt = head.get("format") if head.get("format") in writer.FORMATS else "wav"
        out = writer.target_path(head.get("folder"), head.get("name"), fmt)
        sink = writer.WavSink(out, fmt, int(head.get("sampleRate", 44100)))
        await ws.send_json({"ready": True, "path": str(out)})
        while True:
            msg = await ws.receive()
            if msg.get("bytes"):
                sink.write(msg["bytes"])
            elif msg.get("text"):
                end = json.loads(msg["text"])
                if end.get("end"):
                    res = await run_in_threadpool(sink.close, end.get("tracklist"))
                    sink = None
                    await ws.send_json({"ok": True, **res})
                    break
            elif msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("recording failed")
        try:
            await ws.send_json({"error": str(e)[:300]})
        except Exception:
            pass
    finally:
        if sink is not None:        # 途中で切れた: そこまでの分を保存する
            try:
                sink.close()
            except Exception:
                pass


# ---- 書き出し: 高速（Phase 3 P3-8。design_phase3.md §6.2） ----
@app.post("/api/export/render")
def export_render(body: dict = Body(default={})) -> dict:
    """{items: キューの項目, settings: {lufs_target, xfade_curve, tempo_return, tempo_range}, format, folder, name, tracklist}"""
    from .export import render, writer
    items = body.get("items") or []
    if not items:
        raise HTTPException(400, "ミックスキューが空です")
    fmt = body.get("format") if body.get("format") in writer.FORMATS else "wav"
    st = {**settings.load(), **(body.get("settings") or {})}
    out = writer.target_path(body.get("folder"), body.get("name"), fmt)
    try:
        job = render.start(items, st, out, fmt, bool(body.get("tracklist", True)), None,
                           "browser" if body.get("engine") == "browser" else "python")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"path": str(out), **job.status()}


@app.get("/api/export/status")
def export_status() -> dict:
    from .export import render
    job = render.current()
    return job.status() if job else {"progress": 0, "done": False, "error": None, "result": None}


@app.get("/api/export/segments")
def export_segments() -> dict:
    """画面のストレッチで計算してほしい区間（デッキごと・曲ごと。刻みごとの入力位置つき）。"""
    from .export import render
    job = render.current()
    if job is None or job.stage != "need_audio":
        return {"segments": []}
    return {"segments": job.segment_info()}


@app.post("/api/export/segments/{seg_id}")
async def export_segment_audio(seg_id: int, request: Request) -> dict:
    """画面で計算した区間の音（planar float32）を受け取る。"""
    from .export import render
    job = render.current()
    if job is None or job.stage != "need_audio" or not 0 <= seg_id < len(job.segments):
        raise HTTPException(409, "受け取れる区間がありません")
    data = await request.body()
    try:
        await run_in_threadpool(job.put_audio, seg_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "done": len(job.audio), "total": len(job.segments)}


@app.post("/api/export/cancel")
def export_cancel() -> dict:
    from .export import render
    job = render.current()
    if job:
        job.cancel.set()
    return {"ok": True}


@app.get("/api/export/default-dir")
def export_default_dir() -> dict:
    from .export import writer
    return {"folder": str(writer.default_dir())}


# ---- MIDI・コントローラー割り当て（P1-9） ----
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """画面との双方向通信。サーバー→画面: MIDI 入力。画面→サーバー: MIDI 出力（LED）。"""
    await ws.accept()
    midi.clients.add(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "midi_out":
                midi.send(str(msg.get("port", "")), list(msg.get("data", [])))
    except WebSocketDisconnect:
        pass
    finally:
        midi.clients.discard(ws)


@app.get("/api/midi/ports")
def midi_ports() -> dict:
    return midi.ports()


class MidiInject(BaseModel):
    port: str = "virtual"
    data: list[int]


@app.post("/api/dev/midi-inject")
def midi_inject(body: MidiInject) -> dict:
    """試験用: MIDI 入力を擬似的に流す（実機の入力と同じ経路で画面に届く）。"""
    midi.inject(body.port, body.data)
    return {"ok": True}


_NAME = re.compile(r"^[\w\- ぁ-んァ-ン一-龥]{1,60}$")


@app.get("/api/mappings")
def list_mappings() -> dict:
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    return {"mappings": sorted(p.stem for p in MAPPINGS_DIR.glob("*.json"))}


@app.get("/api/mappings/{name}")
def get_mapping(name: str) -> dict:
    p = MAPPINGS_DIR / f"{name}.json"
    if not _NAME.match(name) or not p.exists():
        return {"name": name, "entries": []}
    return json.loads(p.read_text(encoding="utf-8"))


@app.put("/api/mappings/{name}")
async def put_mapping(name: str, request: Request) -> dict:
    if not _NAME.match(name):
        raise HTTPException(400, "invalid name")
    body = await request.json()
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    (MAPPINGS_DIR / f"{name}.json").write_text(json.dumps({**body, "name": name}, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    return {"ok": True}


# ---- 設定（P1-10） ----
@app.get("/api/settings")
def get_settings() -> dict:
    return settings.load()


@app.put("/api/settings")
async def put_settings(request: Request) -> dict:
    patch = await request.json()
    before = lib_roots.configured()
    if "music_root" in patch:            # Phase 1 の 1 つだけの指定 → 音楽フォルダの一覧にする
        patch["music_roots"] = [patch.pop("music_root")]
    if "music_roots" in patch:
        roots = [os.path.normpath(str(r).strip()) for r in patch["music_roots"] or [] if str(r).strip()]
        known = {lib_roots.norm(r) for r in before}
        for r in roots:                  # 新しく足すフォルダは、今あるものだけ（外付けドライブが外れているだけの既存のフォルダはそのまま）
            if lib_roots.norm(r) not in known and not os.path.isdir(r):
                raise HTTPException(400, f"フォルダが見つかりません: {r}")
        patch["music_roots"] = roots
    cur = settings.save(patch)
    after = lib_roots.configured()
    if [lib_roots.norm(r) for r in after] != [lib_roots.norm(r) for r in before]:
        scanner.set_roots(after)
        scanner.start()
    return cur


@app.get("/api/stats")
def stats() -> dict:
    c = db.connect()
    rw, rargs = lib_roots.where("")
    r = c.execute(f"SELECT COUNT(*), SUM(analysis_status='done'), SUM(analysis_status='failed') FROM tracks WHERE {rw}", rargs).fetchone()
    return {"tracks": r[0], "analyzed": r[1] or 0, "failed": r[2] or 0, "music_root": str(scanner.root) if scanner.root else "", "roots": roots_status()}


def roots_status() -> list[dict]:
    """音楽フォルダごとの状態: 設定にあるもの（見つかるか・曲数）と、外したが曲が残っているもの（隠している曲数）。"""
    c = db.connect()
    counts = {r[0]: r[1] for r in c.execute("SELECT root, COUNT(*) FROM tracks WHERE root IS NOT NULL GROUP BY root")}
    conf = lib_roots.configured()
    out = [{"path": r, "active": True, "exists": os.path.isdir(r), "tracks": counts.get(r, 0)} for r in conf]
    confn = {lib_roots.norm(r) for r in conf}
    out += [{"path": r, "active": False, "exists": os.path.isdir(r), "tracks": n} for r, n in counts.items() if lib_roots.norm(r) not in confn]
    return out


class RevealBody(BaseModel):
    path: str


@app.post("/api/reveal")
def reveal(body: RevealBody) -> dict:
    """書き出したファイル・保存先のフォルダをエクスプローラーで開く（2026-10-03、ユーザーの要望）。"""
    from .procutil import reveal as _reveal
    if not body.path.strip():
        raise HTTPException(400, "パスが空です")
    try:
        return {"opened": _reveal(body.path.strip())}
    except PermissionError:                  # ネットワークのパス（procutil.reveal で正規化してから判定する）
        raise HTTPException(400, "ネットワーク上のフォルダは開けません")
    except FileNotFoundError:
        raise HTTPException(404, "フォルダが見つかりません")


@app.post("/api/dialog/folder")
def dialog_folder() -> dict:
    """専用ウィンドウで開いているときは、Windows のフォルダ選択を開く（ブラウザで開いているときは使えない）。"""
    try:
        import webview
        if not webview.windows:
            raise HTTPException(501, "専用ウィンドウでないため、フォルダ選択は開けません。パスを入力してください")
        r = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(501, f"フォルダ選択を開けませんでした: {e}")
    return {"path": (r[0] if isinstance(r, (list, tuple)) else r) if r else None}


class ClientLog(BaseModel):
    level: str = "error"
    message: str
    detail: str | None = None


@app.post("/api/log")
def client_log(entry: ClientLog) -> dict:
    """画面側のエラーを data/djmix.log に残す（専用ウィンドウでは開発者ツールが見えないため）。"""
    log.log(logging.ERROR if entry.level == "error" else logging.INFO, "frontend %s: %s %s",
            entry.level, entry.message[:500], (entry.detail or "")[:2000])
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    # 開発中に古い画面がキャッシュされないよう no-store
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
