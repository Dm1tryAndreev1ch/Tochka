import asyncio
from types import SimpleNamespace

import anyio
import numpy as np

from app.natural_tts import Engine


async def test_asgi_disconnect_scope_never_strands_tts_lock():
    worker = Engine()
    worker.ready = True

    def generate(text, voice=None, profile=None):
        for _ in range(10000):
            yield SimpleNamespace(audio=np.ones(240, dtype=np.float32) * 0.1)

    worker.generate = generate
    received = anyio.Event()
    response = await worker.stream("Тест", None, "serena")

    async def receive():
        await anyio.sleep_forever()

    async def send(message):
        if message["type"] == "http.response.body":
            received.set()
            await anyio.sleep(0.1)

    async def consumer():
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(consumer)
            await received.wait()
            group.cancel_scope.cancel()
        async with asyncio.timeout(3):
            while worker.generation_lock.locked():
                await asyncio.sleep(0.02)
        assert worker.metrics["cancelled"] == 1
        assert not worker.cancel_events
        # The following utterance must be admitted after an interruption.
        second = await worker.stream("Вторая реплика", None, "serena")
        await second.body_iterator.__anext__()
        await second.body_iterator.aclose()
    finally:
        for stop in worker.cancel_events:
            stop.set()
        await asyncio.to_thread(worker.executor.shutdown, wait=True, cancel_futures=True)
