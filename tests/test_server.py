"""P1-1: サーバーの基本動作。Windows で .js が text/plain になる問題と、キャッシュ(304)の再発防止。"""
from fastapi.testclient import TestClient

from djmix.server import app

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200 and "DJ Mixer" in r.text


def test_module_scripts_have_javascript_mime():
    for path in ("/app.js", "/ui/scale.js", "/vendor/signalsmith-stretch/SignalsmithStretch.mjs"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/javascript"), (path, r.headers["content-type"])


def test_static_never_cached_nor_304():
    r1 = client.get("/app.js")
    assert r1.headers["cache-control"] == "no-store"
    etag = r1.headers.get("etag")
    r2 = client.get("/app.js", headers={"If-None-Match": etag or "x", "If-Modified-Since": r1.headers.get("last-modified", "")})
    assert r2.status_code == 200


def test_diagnostics_endpoint():
    r = client.post("/api/diagnostics", json={"userAgent": "test", "checks": {"AudioWorklet": True, "sampleRate": "44100"}})
    assert r.json() == {"ok": True}
    r = client.post("/api/diagnostics", json={"userAgent": "test", "checks": {"AudioWorklet": False}})
    assert r.json() == {"ok": False}


def test_settings_roundtrip(tmp_path, monkeypatch):
    from djmix import settings
    dbp = tmp_path / "s.sqlite"
    assert settings.load(dbp)["lufs_target"] == -14.0
    cur = settings.save({"lufs_target": -30, "tempo_range": 0.16, "xfade_curve": "bogus", "unknown": 1}, dbp)
    assert cur["lufs_target"] == -23.0 and cur["tempo_range"] == 0.16 and cur["xfade_curve"] == "smooth"
    assert settings.load(dbp)["tempo_range"] == 0.16 and "unknown" not in settings.load(dbp)
    # 繋ぎの後に原速へ戻すか（Phase 2）。既定は戻す
    assert settings.load(dbp)["tempo_return"] is True
    assert settings.save({"tempo_return": False}, dbp)["tempo_return"] is False
    assert settings.load(dbp)["tempo_return"] is False


def test_auto_plan_rejects_unanalyzed_seed(monkeypatch):
    from djmix.planner import engine
    monkeypatch.setattr(engine, "get_pool", lambda db_path=None: engine.Pool({}))    # 実データの DB は使わない
    r = client.post("/api/auto/plan", json={"mode": "library", "seed": 987654321, "groups": [], "minutes": 20})
    assert r.status_code == 400


def test_rejects_requests_from_other_sites(monkeypatch):
    """公開にあたっての見直し（2026-11-01）: 別のサイトの画面からの要求（CSRF・DNS rebinding・WebSocket の乗っ取り）を断る。"""
    import pytest
    from starlette.websockets import WebSocketDisconnect
    from djmix import procutil
    monkeypatch.setattr(procutil.subprocess, "Popen", lambda *a, **k: None)     # 試験でエクスプローラーを開かない
    own = TestClient(app, base_url="http://127.0.0.1:8766")
    assert own.get("/api/health", headers={"Origin": "http://127.0.0.1:8766"}).status_code == 200       # 自分の画面
    assert TestClient(app, base_url="http://localhost:8768").get("/api/health", headers={"Origin": "http://localhost:8768"}).status_code == 200
    assert own.get("/api/health", headers={"Origin": "http://127.0.0.1:3000"}).status_code == 403       # 同じ PC の別のポートの画面
    assert own.get("/api/health", headers={"Origin": "http://localhost:8766"}).status_code == 403
    with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}):    # TestClient の WebSocket は Host が testserver
        pass
    assert client.post("/api/reveal", json={"path": "C:/"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.example:8766"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "http://127.0.0.1.evil.example"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/record", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    B = chr(92)
    for unc in (B * 2 + "server" + B + "share", "/" + B + "server" + B + "share", "//server/share",
                B * 2 + "?" + B + "UNC" + B + "server" + B + "share", " " + B * 2 + "server" + B + "x"):
        assert client.post("/api/reveal", json={"path": unc}).status_code == 400, unc   # ネットワークのパスは書き方を変えても開かない


def test_export_name_stays_in_folder(tmp_path):
    from djmix.export import writer
    p = writer.target_path(str(tmp_path), r"..\..\evil", "wav")
    assert p.parent == tmp_path and "\\" not in p.name and "/" not in p.name     # フォルダの外へ出ない
