"""P1-4: 再生用 PCM の配信。解析と同じデコード結果を planar float32 で切り出す。"""
import numpy as np
import soundfile as sf

from djmix.audio.decode import decode
from djmix.audio.pcm import PcmStore


def test_chunk_is_planar_and_matches_full_decode(tmp_path):
    p = tmp_path / "t.wav"
    sr = 44100
    t = np.arange(sr * 3) / sr
    st = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 660 * t)], axis=1).astype(np.float32) * 0.5
    sf.write(p, st, sr)
    full = decode(p)
    store = PcmStore(max_tracks=2)
    body, total = store.chunk(1, str(p), offset=sr, frames=1000)
    assert total == len(full)
    x = np.frombuffer(body, dtype=np.float32)
    L, R = x[:1000], x[1000:]
    assert np.array_equal(L, full[sr:sr + 1000, 0]) and np.array_equal(R, full[sr:sr + 1000, 1])
    # 末尾を越える要求は短くなる
    body, _ = store.chunk(1, str(p), offset=len(full) - 10, frames=1000)
    assert len(body) == 10 * 2 * 4


def test_lru_keeps_limited_tracks(tmp_path):
    store = PcmStore(max_tracks=2)
    paths = []
    for i in range(3):
        p = tmp_path / f"{i}.wav"
        sf.write(p, np.zeros((441, 2), dtype=np.float32), 44100)
        paths.append(p)
        store.get(i, str(p))
    assert list(store.data.keys()) == [1, 2]
