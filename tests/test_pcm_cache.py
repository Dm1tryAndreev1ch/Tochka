from types import SimpleNamespace

import numpy as np

from app.config import settings
from app.natural_tts import Engine
from app.pcm_cache import PCMCache


def test_cache_evicts_least_recent_audio_within_both_limits():
    cache = PCMCache(max_bytes=8, max_entries=2)
    cache.put("one", b"1234")
    cache.put("two", b"5678")
    assert cache.get("one") == b"1234"
    cache.put("three", b"abcd")
    assert cache.get("two") is None
    assert cache.bytes == 8
    cache.put("one", b"12")
    assert cache.bytes == 6
    cache.put("huge", b"0" * 9)
    assert cache.bytes == 6
    cache.clear()
    assert cache.bytes == 0 and not cache.entries


async def collect(response):
    return b"".join([chunk async for chunk in response.body_iterator])


def engine(monkeypatch):
    monkeypatch.setattr(settings, "male_speech_rate", 1)
    worker = Engine()
    worker.ready = True
    worker.calls = []

    def generate(text, voice=None, profile=None):
        worker.calls.append((text, voice, profile))
        for _ in range(3):
            yield SimpleNamespace(audio=np.full(240, 0.1, dtype=np.float32))

    worker.generate = generate
    return worker


async def test_public_audio_cache_hit_bypasses_busy_model_and_preserves_pcm(monkeypatch):
    worker = engine(monkeypatch)
    try:
        first = await collect(await worker.stream("Здравствуйте.", None, profile="male", cache=True))
        await worker.generation_lock.acquire()
        try:
            repeated = await collect(await worker.stream("Здравствуйте.", None, profile="male", cache=True))
        finally:
            worker.generation_lock.release()
        assert first == repeated and len(first) == 1440
        assert len(worker.calls) == 1
        assert worker.audio_cache.hits == 1
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)


async def test_private_or_interrupted_audio_is_never_cached(monkeypatch):
    worker = engine(monkeypatch)
    try:
        await collect(await worker.stream("Личная реплика.", None, profile="male"))
        await collect(await worker.stream("Личная реплика.", None, profile="male"))
        assert len(worker.calls) == 2 and not worker.audio_cache.entries
        response = await worker.stream("Прерванная реплика.", None, profile="male", cache=True)
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        assert not worker.audio_cache.entries
        await collect(await worker.stream("Прерванная реплика.", None, profile="male", cache=True))
        assert len(worker.calls) == 4
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)


async def test_cached_audio_is_separate_for_voice_text_and_tempo(monkeypatch):
    worker = engine(monkeypatch)
    try:
        for text, profile in [("Здравствуйте.", "male"), ("Здравствуйте.", "female"), ("Другой ответ.", "male")]:
            await collect(await worker.stream(text, None, profile=profile, cache=True))
        assert len(worker.calls) == 3
        assert len(worker.audio_cache.entries) == 3
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)
