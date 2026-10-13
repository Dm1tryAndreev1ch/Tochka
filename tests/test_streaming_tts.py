from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.config import settings
from app.errors import AppError
from app.models import LocalModels
from app.natural_tts import Engine


class BytesStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for chunk in [b"\x01", b"\x00\x02", b"\x00"]:
            yield chunk


async def test_pcm_chunks_preserve_sample_boundaries_and_profile(monkeypatch):
    monkeypatch.setattr(settings, "natural_tts_enabled", True)
    observed = []

    def handler(request):
        observed.append(request.content)
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True, "voice": "serena"})
        return httpx.Response(
            200, stream=BytesStream(), headers={"X-Audio-Sample-Rate": "24000", "X-Audio-Format": "pcm_s16le"}
        )

    model = LocalModels()
    model.ready = True
    await model.natural_http.aclose()
    model.natural_http = httpx.AsyncClient(base_url="http://tts", transport=httpx.MockTransport(handler))
    try:
        audio = b"".join([chunk async for chunk in model.stream_speech("Здравствуйте", "female")])
        assert audio == b"\x01\x00\x02\x00"
        assert b"serena" in observed[-1]
        assert model.stats["tts_first_audio_seconds"]
    finally:
        await model.close()


async def test_female_failure_does_not_silently_change_to_male(monkeypatch):
    monkeypatch.setattr(settings, "natural_tts_enabled", False)
    model = LocalModels()
    model.ready = True
    try:
        with pytest.raises(AppError, match="Женский голос"):
            _ = [chunk async for chunk in model.stream_speech("Здравствуйте", "female")]
    finally:
        await model.close()


async def test_cancelled_native_generator_releases_worker_slot(monkeypatch):
    worker = Engine()
    worker.ready = True

    def generate(text, voice=None, profile=None):
        for _ in range(500):
            yield SimpleNamespace(audio=np.ones(240, dtype=np.float32) * 0.1)

    worker.generate = generate
    try:
        response = await worker.stream("Тест", None, "serena")
        iterator = response.body_iterator
        chunk = await iterator.__anext__()
        assert len(chunk) == 480
        await iterator.aclose()
        assert not worker.generation_lock.locked()
        assert worker.metrics["cancelled"] == 1
        assert not worker.cancel_events
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)
