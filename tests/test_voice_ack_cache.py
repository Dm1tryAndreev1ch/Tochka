import asyncio
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import LocalModels
from app.natural_tts import Engine
from app.voice_phrases import ACKNOWLEDGEMENT, ACKNOWLEDGEMENT_MAX_BYTES

HEADERS = {
    "X-Audio-Sample-Rate": "24000", "X-Audio-Format": "pcm_s16le",
    "X-Acknowledgement-Cached": "1", "X-TTS-Generation": "worker-1",
}


async def replace_client(model, handler):
    await model.natural_http.aclose()
    model.natural_http = httpx.AsyncClient(base_url="http://speech.test", transport=httpx.MockTransport(handler))


async def test_ack_cache_fetches_only_prepared_audio_and_keeps_profiles_separate(monkeypatch):
    monkeypatch.setattr(settings, "natural_tts_enabled", True)
    requests = []

    def handler(request):
        import json

        requests.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={
                "ready": True, "voice": "reference", "generation_id": "worker-1",
                "acknowledgement_profiles": ["male", "female"],
            })
        body = json.loads(request.content)
        assert body == {"text": ACKNOWLEDGEMENT, "profile": body["profile"], "cache": True}
        return httpx.Response(200, content=b"\x01\x00" if body["profile"] == "male" else b"\x02\x00", headers=HEADERS)

    model = LocalModels()
    await replace_client(model, handler)
    try:
        await model.tts_status(force=True)
        await model._acknowledgement_task
        assert model.cached_acknowledgement("male") == b"\x01\x00"
        assert model.cached_acknowledgement("female") == b"\x02\x00"
        assert model.cached_acknowledgement("unknown") is None
        count = len(requests)
        for _ in range(20):
            assert model.cached_acknowledgement("male") == b"\x01\x00"
        assert len(requests) == count
        monkeypatch.setattr(settings, "voice_acknowledgement_enabled", False)
        assert model.cached_acknowledgement("male") is None
    finally:
        await model.close()


@pytest.mark.parametrize("content,headers", [
    (b"\x01\x00", {**HEADERS, "X-Acknowledgement-Cached": "0"}),
    (b"\x01\x00", {**HEADERS, "X-TTS-Generation": "different-worker"}),
    (b"\x01", HEADERS),
    (b"", HEADERS),
    (b"\x01\x00" * (ACKNOWLEDGEMENT_MAX_BYTES // 2 + 1), HEADERS),
])
async def test_ack_cache_rejects_unprepared_stale_or_invalid_pcm(content, headers):
    model = LocalModels()
    model.natural_status = {"natural_ready": True}
    model._acknowledgement_generation = "worker-1"
    await replace_client(model, lambda request: httpx.Response(200, content=content, headers=headers))
    try:
        await model._warm_acknowledgements("worker-1", ["male"])
        assert model.cached_acknowledgement("male") is None
    finally:
        await model.close()


async def test_speech_worker_restart_invalidates_prepared_acknowledgement(monkeypatch):
    monkeypatch.setattr(settings, "natural_tts_enabled", True)
    model = LocalModels()
    model.natural_status = {"natural_ready": True}
    model._acknowledgement_generation = "worker-1"
    model._acknowledgements["male"] = b"\x01\x00"
    await replace_client(model, lambda request: httpx.Response(200, json={
        "ready": True, "voice": "reference", "generation_id": "worker-2", "acknowledgement_profiles": [],
    }))
    try:
        await model.tts_status(force=True)
        assert model.cached_acknowledgement("male") is None
    finally:
        await model.close()


async def test_closing_models_cancels_ack_copy_and_clears_audio():
    waiting = asyncio.Event()

    async def handler(request):
        waiting.set()
        await asyncio.Event().wait()

    model = LocalModels()
    model._acknowledgement_generation = "worker-1"
    model._acknowledgements["female"] = b"\x01\x00"
    await replace_client(model, handler)
    task = model._acknowledgement_task = asyncio.create_task(model._warm_acknowledgements("worker-1", ["male"]))
    await waiting.wait()
    await model.close()
    assert task.cancelled()
    assert not model._acknowledgements


async def test_prepared_phrase_does_not_use_generator_or_worker_slot():
    worker = Engine()
    worker.ready = True
    worker.acknowledgements["female"] = b"\x01\x00" * 2400
    worker.generate = lambda *args: pytest.fail("Prewarmed acknowledgement must not run inference")
    await worker.generation_lock.acquire()
    try:
        response = await worker.stream(ACKNOWLEDGEMENT, None, profile="female", cache=True)
        data = b"".join([chunk async for chunk in response.body_iterator])
        assert data == worker.acknowledgements["female"]
        assert response.headers["X-Acknowledgement-Cached"] == "1"
        assert worker.generation_lock.locked()
    finally:
        worker.generation_lock.release()
        worker.executor.shutdown(wait=True, cancel_futures=True)


async def test_missing_prepared_phrase_cannot_launch_inference():
    worker = Engine()
    worker.ready = True
    worker.generate = lambda *args: pytest.fail("An acknowledgement cache miss must not run inference")
    try:
        with pytest.raises(HTTPException) as error:
            await worker.stream(ACKNOWLEDGEMENT, None, profile="male", cache=True)
        assert error.value.status_code == 503
        assert not worker.generation_lock.locked()
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)


def test_only_complete_bounded_acknowledgements_are_pinned(monkeypatch):
    monkeypatch.setattr(settings, "male_speech_rate", 1)
    worker = Engine()
    closed = []

    def generate(*args, **kwargs):
        try:
            for _ in range(4):
                yield SimpleNamespace(audio=np.ones(24000, dtype=np.float32) * .1)
        finally:
            closed.append(True)

    worker.generate = generate
    try:
        worker._warm_acknowledgement("male")
        assert worker.acknowledgements == {}
        assert closed == [True]
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)
