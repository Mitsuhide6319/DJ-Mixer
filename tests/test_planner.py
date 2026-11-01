"""P3-2〜P3-4: おまかせの要約・相性・並びの探索（design_phase3.md §3.2・§4）。合成データで試す。"""
import pytest
import sqlite3

import numpy as np

from djmix import db
from djmix.analysis.features import BAR_COLUMNS
from djmix.planner import desc, genres


def synth(n=200, bpm=120.0, no_beat=(), key="8B", extra=None, seed=1):
    """一定テンポの曲の解析結果と小節ごとの特徴量。途中（n/2〜）だけ盛り上がる。"""
    rng = np.random.default_rng(seed)
    bar = 4 * 60 / bpm
    feats = np.zeros((n, len(BAR_COLUMNS)), dtype=np.float32)
    level = np.ones(n) * 0.3
    level[n // 2:] = 0.9
    feats[:, 0] = level + rng.normal(0, 0.02, n)
    feats[:, 1] = level * 0.8
    feats[:, 2:14] = rng.random((n, 12)) * 0.1 + 0.5
    feats[:, -1] = 0.3
    a = {"bpm": bpm, "meter": 4, "duration": n * bar, "bars": [{"beat": i * 4, "time": round(i * bar, 3)} for i in range(n)],
         "no_beat": [list(x) for x in no_beat], "beat_class": "A",
         "key": {"camelot": key}, "key_head30": {"camelot": key}, "key_tail30": {"camelot": key},
         "extra": extra or {"centroid": 1500.0, "low_ratio": 0.2, "onset_rate": 3.0}}
    return a, feats


def test_genre_groups():
    assert genres.group_of("Jazz & Fusion") == genres.group_of("Smooth Jazz") == "ジャズ・フュージョン"
    assert genres.group_of("Domestic(J-Pops)") == "J-POP・歌謡"
    assert genres.group_of("Pop/Rock") == "ポップス" and genres.group_of(None) == "その他"


def test_use_range_is_cut_for_long_track_and_fadeout():
    # 200 小節 × 2 秒 = 400 秒。最後の 6 秒は拍が取れない（フェードアウト）
    a, feats = synth(no_beat=[(394.0, 400.0)])
    d = desc.build(a, feats)
    u = d["use"]
    assert u["t1"] - u["t0"] <= desc.MAX_USE_SEC + 1 and u["t1"] - u["t0"] >= desc.MIN_USE_SEC
    assert u["t1"] <= 394.0 and u["bar1"] > u["bar0"]
    assert abs(u["t0"] / 2 - round(u["t0"] / 2)) < 1e-6          # 端は小節頭
    assert d["out"]["nobeat"] == 0 and len(d["in"]["chroma"]) == 12


def test_short_track_keeps_whole_and_bad_track_is_skipped():
    a, feats = synth(n=100)                                       # 200 秒
    u = desc.build(a, feats)["use"]
    assert u["t0"] == 0 and u["t1"] == 200.0
    a2, f2 = synth(n=10)
    assert desc.build(a2, f2) is None                              # 小節が少なすぎる


def test_store_and_load(tmp_path):
    dbp = tmp_path / "t.sqlite"
    c = db.connect(dbp)
    c.execute("INSERT INTO tracks(id, path, folder, title, genre, analysis_status) VALUES(1, 'a.mp3', 'x', 'A', 'Jazz', 'done')")
    c.commit()
    a, feats = synth()
    desc.update(1, a, feats, dbp)
    got = desc.load_all(dbp)
    assert got[1]["title"] == "A" and got[1]["bpm"] == 120.0 and "use" in got[1]


# ---- P3-4: 相性と探索（合成の要約で試す） ----
from djmix.planner import engine  # noqa: E402


def fake_desc(bpm, key="8B", genre="Jazz", artist="A", nobeat=0.0, cen=1500.0, ons=3.0, use_len=240.0, seed=0):
    rng = np.random.default_rng(seed)
    ch = list(rng.random(12) * 0.1 + 0.5)
    reg = {"chroma": ch, "harm": 0.4, "low": 0.5, "nobeat": nobeat}
    return {"bpm": bpm, "meter": 4, "duration": use_len + 20, "beat_class": "A", "key": key, "key_head": key, "key_tail": key,
            "extra": {"centroid": cen, "low_ratio": 0.2, "onset_rate": ons},
            "use": {"t0": 0.0, "t1": use_len, "bar0": 0, "bar1": int(use_len / (240 / bpm)), "notes": []},
            "in": dict(reg), "out": dict(reg), "title": f"t{seed}", "artist": artist, "genre": genre}


def make_pool(monkeypatch, descs, ratings=None, recent=()):
    from djmix import prefs
    pool = engine.Pool(descs)
    monkeypatch.setattr(engine, "get_pool", lambda db_path=None: pool)
    monkeypatch.setattr(engine, "_envelope", lambda tid, use: [])
    monkeypatch.setattr(prefs, "ratings", lambda db_path=None: dict(ratings or {}))      # 実データの DB を読まない
    monkeypatch.setattr(prefs, "recent", lambda days, db_path=None: set(recent))
    return pool


def test_pair_scores(monkeypatch):
    descs = {1: fake_desc(120), 2: fake_desc(121, seed=2), 3: fake_desc(60.5, seed=3), 4: fake_desc(120, key="3A", seed=4),
             5: fake_desc(121, nobeat=4.0, seed=5), 6: fake_desc(90, seed=6)}
    pool = make_pool(monkeypatch, descs)
    M = engine.Matrix(pool, list(descs))
    s = lambda a, b: M.S[M.pos[a], M.pos[b]]  # noqa: E731
    assert s(1, 2) > s(1, 4)                                    # キーが離れていると下がる
    assert M.k[M.pos[1], M.pos[3]] == 0.5 and M.ts[M.pos[1], M.pos[3]] == 1.0   # 半分のテンポは ×½ で合わせる
    assert M.join(1, 5)["tpl"] == "echo" and "エコーアウト" in " ".join(M.join(1, 5)["why"])   # 拍が取れない
    assert M.join(1, 2)["tpl"] == "long" and M.join(1, 2)["bars"] == 16
    assert s(1, 6) < s(1, 2)                                    # 90→120 は離れている


def many(n=40):
    rng = np.random.default_rng(5)
    keys = ["8B", "9B", "7B", "8A", "3A"]
    return {i: fake_desc(float(rng.uniform(105, 130)), key=keys[i % 5], genre="Jazz" if i % 4 else "Rock",
                         artist=f"a{i % 13}", cen=float(rng.uniform(800, 3000)), ons=float(rng.uniform(1, 6)),
                         use_len=float(rng.uniform(200, 300)), seed=i) for i in range(1, n + 1)}


def test_plan_library_three_distinct_plans(monkeypatch):
    make_pool(monkeypatch, many())
    r = engine.plan_library(1, ["ジャズ・フュージョン"], 20, "up")
    assert len(r["plans"]) == 3
    for p in r["plans"]:
        assert p["tracks"][0]["id"] == 1 and p["minutes"] >= 20
        assert all(t["genre"] == "Jazz" for t in p["tracks"][1:])          # 範囲の曲だけ
        assert len({t["id"] for t in p["tracks"]}) == len(p["tracks"])   # 同じ曲は 2 回使わない
        assert all("join" in t for t in p["tracks"][:-1]) and "join" not in p["tracks"][-1]
    a, b = ({t["id"] for t in p["tracks"][1:]} for p in r["plans"][:2])
    assert len(a & b) <= 0.5 * max(len(a), len(b))                      # 互いに半分以上違う


def test_plan_queue_keeps_tracks_and_improves(monkeypatch):
    make_pool(monkeypatch, many())
    ids = [3, 17, 8, 22, 11, 30, 5]
    r = engine.plan_queue(ids)
    best, cur = r["plans"]
    assert sorted(t["id"] for t in best["tracks"]) == sorted(ids)
    assert [t["id"] for t in cur["tracks"]] == ids and best["score"] >= cur["score"]


def test_next_candidates(monkeypatch):
    make_pool(monkeypatch, many())
    c = engine.next_candidates(1, None, [], [2, 3], n=5)
    assert len(c) == 5 and all(x["id"] not in (1, 2, 3) for x in c)
    assert [x["score"] for x in c] == sorted((x["score"] for x in c), reverse=True)
    r = engine.next_candidates(1, 9, [], [], n=5)                       # 差し替え: 前後の相性の平均
    assert all("join" in x and "join_next" in x for x in r)


def test_plan_follows_drawn_curve(monkeypatch):
    make_pool(monkeypatch, many(60))
    up = engine.plan_library(1, [], 25, "custom", curve=[0.1, 0.9])["plans"][0]
    down = engine.plan_library(1, [], 25, "custom", curve=[0.9, 0.1])["plans"][0]
    e_up = [t["energy"] for t in up["tracks"][1:]]
    e_dn = [t["energy"] for t in down["tracks"][1:]]
    # 描いた線の向きに、後半の曲のエネルギーが寄る
    assert np.mean(e_up[len(e_up) // 2:]) > np.mean(e_dn[len(e_dn) // 2:])



def test_prefs_skip_and_prefer(monkeypatch):
    base = engine.plan_library  # noqa: F841
    make_pool(monkeypatch, many(60))
    first = engine.plan_library(1, [], 25, "keep")["plans"][0]
    used = [t["id"] for t in first["tracks"][1:]]
    # ★1 と最近かけた曲は使わない（起点の曲は除く）
    make_pool(monkeypatch, many(60), ratings={used[0]: 1}, recent=[used[1], 1])
    r = engine.plan_library(1, [], 25, "keep", prefs_opt={"skip_rating1": True, "avoid_recent_days": 14})
    ids = {t["id"] for p in r["plans"] for t in p["tracks"]}
    assert used[0] not in ids and used[1] not in ids and r["plans"][0]["tracks"][0]["id"] == 1
    # ★5 を付けた曲は選ばれやすくなる
    unused = [t for t in range(2, 61) if t not in {x["id"] for p in engine.plan_library(1, [], 25, "keep")["plans"] for x in p["tracks"]}]
    make_pool(monkeypatch, many(60), ratings={t: 5 for t in unused[:15]})
    r = engine.plan_library(1, [], 25, "keep", prefs_opt={"prefer_rating": True})
    assert any(t["id"] in unused[:15] for t in r["plans"][0]["tracks"])


def test_auto_ramp_shift_loop(monkeypatch):
    # 1→2: テンポ差 10%（ランプ）、キー 8B→3B は合わないが 3B を −1 半音で 8B（同じ）、2 は使う範囲が短い（ループ）
    descs = {1: fake_desc(120, key="8B", seed=1), 2: fake_desc(109, key="3B", seed=2, use_len=40), 3: fake_desc(110, key="9B", seed=3)}
    pool = make_pool(monkeypatch, descs)
    M = engine.Matrix(pool, [1, 2, 3])
    assert M.ramp[0, 1] and M.join(1, 2)["tpl"] == "long" and M.join(1, 2)["params"]["tempo"] == "ramp"
    p = engine._pack(M, [0, 1, 2], with_env=False)
    j1, j2 = p["tracks"][0]["join"], p["tracks"][1]["join"]
    assert j1["params"]["shift"] == -1 and any("-1 半音" in w for w in j1["why"])
    assert engine.shift_camelot("3B", -1) == "8B" and engine.shift_camelot("3B", 1) == "10B"
    # 2 は −1 半音ずれたまま（末尾 8B）→ 3 は 9B（隣）なので、ずらさずに合う
    assert j2["params"]["shift"] == 0
    assert j2["params"]["loop"] == 4                               # 2 の使う範囲が短い


def test_vocal_fit_and_clash(monkeypatch):
    # モックアップの例と同じ形: 前の曲は最後の 6 小節以外ほぼ歌、次の曲は最初の 8 小節が歌なし
    vo = [0.8] * 60 + [0.0] * 6
    vi = [0.0] * 8 + [0.8] * 40
    fit = engine.best_vocal_fit(vo, vi, 16)
    assert fit["clash0"] > 0 and fit["clash"] == 0 and fit["bars"] == 12 and fit["trim"] == 0     # 12 小節でも重ならない（8 より長く重ねられる）
    assert engine.best_vocal_fit([0.0] * 40, vi, 16)["clash0"] == 0          # インストなら何もしない
    d1, d2, d3 = fake_desc(120, seed=1), fake_desc(120, seed=2), fake_desc(120, seed=3)
    d1["vocals"], d2["vocals"], d3["vocals"] = [0.9] * 40, [0.9] * 40, [0.0] * 40
    pool = make_pool(monkeypatch, {1: d1, 2: d2, 3: d3})
    M = engine.Matrix(pool, [1, 2, 3])
    assert M.vclash[0, 1] == 1.0 and M.vclash[0, 2] == 0.0
    assert M.S[0, 1] < M.S[0, 2] and any("ボーカル" in w for w in M.join(1, 2)["why"])


def test_stable_bounds_moves_join_to_steady_beats():
    """Phase 5 F: 繋ぐ部分（終わり・始まりの 16 小節）に拍の取れない所があれば、拍の取れる所までずらす。"""
    from djmix.planner.desc import stable_bounds
    bt = [i * 2.0 for i in range(201)]                         # 2 秒の小節 × 200（400 秒）
    a = {"no_beat": [(360.0, 400.0), (0.0, 20.0)]}               # 最後の 40 秒・最初の 20 秒は拍なし
    notes = []
    b0, b1 = stable_bounds(a, bt, 0, 200, notes)
    assert b1 == 180 and b0 == 10                                # 360 秒で終える・20 秒から始める
    assert notes == ["拍の取れる所で終える", "拍の取れる所から始める"]
    # ずらせる上限（max(90 秒, 40%)）を超えるなら、そのまま
    a2 = {"no_beat": [(100.0, 400.0)]}
    assert stable_bounds(a2, bt, 0, 200) == (0, 200)
    # 拍が取れていればそのまま
    assert stable_bounds({"no_beat": []}, bt, 0, 200) == (0, 200)


def test_choose_parts():
    """Phase 5 B2: ロングは入れ替え、歌が重なるショートは歌を先に抜く。設定で切れる。"""
    from types import SimpleNamespace
    from djmix.planner.engine import _choose_parts
    M = SimpleNamespace(auto_parts=True)
    j = {"tpl": "long", "bars": 16, "params": {}, "why": []}
    _choose_parts(M, {}, {}, j)
    assert j["params"]["parts"] == "swap"
    voc = {"vocals": [0.9] * 40}
    j = {"tpl": "short", "bars": 8, "params": {}, "why": []}
    _choose_parts(M, voc, voc, j)
    assert j["params"]["parts"] == "vocal"
    j = {"tpl": "echo", "bars": 2, "params": {}, "why": []}
    _choose_parts(M, voc, voc, j)
    assert j["params"]["parts"] == "none"
    j = {"tpl": "long", "bars": 16, "params": {}, "why": []}
    _choose_parts(SimpleNamespace(auto_parts=False), {}, {}, j)
    assert j["params"]["parts"] == "none"


def test_next_candidates_filter_artist_and_genre(monkeypatch):
    """次の曲の候補をアーティスト・ジャンルのまとまりで絞る（大文字小文字・空白は区別しない）。"""
    make_pool(monkeypatch, many())
    c = engine.next_candidates(1, None, [], [], n=20, artists=["A3 ", "a5"])
    assert c and {x["artist"] for x in c} <= {"a3", "a5"}
    g = engine.next_candidates(1, None, ["ロック"], [], n=20)
    assert g and all(x["genre"] == "Rock" for x in g)
    both = engine.next_candidates(1, None, ["ロック"], [], n=20, artists=["a4", "a8"])
    assert all(x["genre"] == "Rock" and x["artist"] in ("a4", "a8") for x in both)
    f = engine.candidate_facets()
    assert {a["name"] for a in f["artists"]} == {f"a{i}" for i in range(13)} and sum(a["count"] for a in f["artists"]) == 40
    assert {g["name"] for g in f["groups"]} == {"ジャズ・フュージョン", "ロック"}


def test_first_track_starts_from_top(monkeypatch):
    """ミックスの最初の曲は、短い前奏（30 秒以下）を飛ばさず 0:00 から。2 曲目以降は使う範囲のまま（小節頭から）。"""
    ds = many()
    for d in ds.values():
        d["use"] = {**d["use"], "t0": 1.42}
    make_pool(monkeypatch, ds)
    r = engine.plan_queue([3, 17, 8])
    t = r["plans"][0]["tracks"]
    assert t[0]["use"]["t0"] == 0.0 and "最初の曲なので冒頭から" in t[0]["use"]["notes"]
    assert all(x["use"]["t0"] == 1.42 for x in t[1:])


def test_short_tracks_get_short_overlaps(monkeypatch):
    """短くたくさん繋ぐ（2026-10-25）: 1 曲 90 秒以下を選んだ曲は、重なりを短い方の範囲の 1/3 まで（4 小節単位、最低 4）。ループで延ばさない。"""
    descs = {k: fake_desc(120, seed=k, use_len=40) for k in (1, 2, 3)}       # 40 秒 = 20 小節
    for d in descs.values():
        d["seg"] = {"len": 45, "pos": "auto"}
    pool = make_pool(monkeypatch, descs)
    M = engine.Matrix(pool, [1, 2, 3])
    j = M.join(1, 2)
    assert j["tpl"] == "short" and j["bars"] == 4 and any("曲が短い" in w for w in j["why"])
    assert abs(M.overlap_sec(0, 1) - 4 * 2.0) < 1e-9
    p = engine._pack(M, [0, 1, 2], with_env=False)
    assert all(t["join"]["params"]["loop"] == 0 for t in p["tracks"][:-1])
    # 長さを選んでいない曲は今までどおり（標準の重なり）
    descs2 = {k: fake_desc(120, seed=k, use_len=40) for k in (1, 2)}
    M2 = engine.Matrix(make_pool(monkeypatch, descs2), [1, 2])
    assert M2.join(1, 2)["bars"] in (8, 16)


def album_pool(monkeypatch, sizes=(6, 6, 6, 2), bpms=(118, 122, 90, 140)):
    """アルバムで選ぶ（2026-10-25）の試験用: アルバム A〜D、テンポもばらばら。"""
    descs, k = {}, 1
    for a, (n, bpm) in enumerate(zip(sizes, bpms)):
        for _ in range(n):
            d = fake_desc(bpm + k % 3, seed=k, artist=f"art{a}", use_len=200)
            d["album"] = "ABCD"[a]
            descs[k] = d
            k += 1
    return make_pool(monkeypatch, descs)


def test_plan_albums_covers_every_album_and_ignores_tempo(monkeypatch):
    album_pool(monkeypatch)
    r = engine.plan_albums(["A", "B", "C", "D"], 30, "up")
    assert r["plans"] and r["albums"] == 4 and not r["missing_albums"]
    for p in r["plans"]:
        alb = [engine.get_pool().d[t["id"]]["album"] for t in p["tracks"]]
        assert set(alb) == {"A", "B", "C", "D"}                     # どのアルバムからも入る（テンポの離れた C・D も）
        assert all(v >= 1 for v in p["albums"].values())
    firsts = {p["tracks"][0]["id"] for p in r["plans"]}
    assert len(firsts) >= 2                                        # 起点の曲は自動で、案ごとに違う


def test_plan_albums_seed_use_all_and_errors(monkeypatch):
    album_pool(monkeypatch)
    r = engine.plan_albums(["A", "B", "C"], 30, "keep", seed=7)
    assert all(p["tracks"][0]["id"] == 7 for p in r["plans"])
    r = engine.plan_albums(["A", "B", "C", "D"], 5, "up", use_all=True)
    assert all(len(p["tracks"]) == 20 for p in r["plans"]) and r["max_minutes"] > 30
    assert len({tuple(t["id"] for t in p["tracks"]) for p in r["plans"]}) == len(r["plans"])
    r = engine.plan_albums(["A", "B", "C"], 600, "up")             # 曲が足りない → そこまでの並び＋使える長さの目安
    assert r["plans"] and r["max_minutes"] < 600
    with pytest.raises(ValueError):
        engine.plan_albums(["A", "B"], 30, "up")
    with pytest.raises(ValueError):
        engine.plan_albums(["A", "B", "Z"], 30, "up")
