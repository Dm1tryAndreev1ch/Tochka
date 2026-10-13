"""Loopback-only, authenticated MLX Qwen TTS with bounded PCM streaming."""

import asyncio
import contextlib
import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.audio_tempo import SpeechTempo
from app.config import ROOT, settings
from app.telemetry import event, failure

logging.basicConfig(level=logging.INFO, format="%(message)s")


class PCMResponse(StreamingResponse):
    def __init__(self, *args, finalize, **kwargs):
        super().__init__(*args, **kwargs)
        self.finalize = finalize

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Closing the ASGI response must stop inference even if cancellation
            # happened in send(), while the audio generator was suspended at yield.
            cleanup = self.finalize()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await self.body_iterator.aclose()


class Synthesis(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: Literal["ryan", "aiden", "uncle_fu", "serena", "vivian"] | None = None
    profile: Literal["male", "female"] | None = None


class Engine:
    def __init__(self):
        self.model = None
        self.ready = False
        self.error = None
        self.references = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="natural-tts")
        self.generation_lock = asyncio.Lock()
        self.cancel_events = set()
        self.cleanup_tasks = set()
        self.metrics = {"completed": 0, "cancelled": 0, "errors": 0, "first_audio_seconds": [], "rtf": []}

    def load(self):
        try:
            import mlx.core as mx
            from mlx_audio.tts.utils import load_model

            path = ROOT / settings.natural_tts_model
            if not (path / "speech_tokenizer/model.safetensors").exists():
                raise FileNotFoundError("Natural TTS weights missing")
            started = time.perf_counter()
            self.model = load_model(str(path))
            if self.model.config.tts_model_type == "base":
                reference_dir = ROOT / settings.voice_reference_dir
                self.references = json.loads((reference_dir / "metadata.json").read_text())
                for profile in ("male", "female"):
                    reference = self.references[profile]
                    reference["path"] = str(reference_dir / reference["file"])
                    if not Path(reference["path"]).is_file():
                        raise FileNotFoundError("Voice reference missing")
            # Compile the same streaming path used by the application.
            for profile in ("male", "female"):
                for result in self.generate("Здравствуйте.", profile=profile):
                    mx.eval(result.audio)
            self.ready = True
            event(
                "natural_tts_ready",
                model="Qwen3-TTS-0.6B-8bit",
                load_seconds=round(time.perf_counter() - started, 3),
            )
        except Exception as exc:
            self.error = type(exc).__name__
            failure("natural-tts", self.error)
            event("natural_tts_unavailable", error_type=self.error)

    def generate(self, text, voice=None, profile=None):
        profile = profile or ("female" if voice == settings.natural_tts_female_voice else "male")
        reference = self.references.get(profile)
        parameters = (
            {"ref_audio": reference["path"], "ref_text": reference["text"]}
            if reference
            else {
                "voice": voice
                or (
                    settings.natural_tts_female_voice
                    if profile == "female"
                    else settings.natural_tts_male_voice
                )
            }
        )
        return self.model.generate(
            text=text,
            **parameters,
            lang_code="russian",
            stream=True,
            streaming_interval=settings.tts_streaming_interval,
            temperature=0.6,
            top_p=0.95,
            repetition_penalty=1.05,
            max_tokens=500,
            verbose=False,
        )

    async def stream(self, text, request, voice=None, profile=None):
        if not self.ready:
            raise HTTPException(503, "Natural TTS is not ready")
        try:
            await asyncio.wait_for(self.generation_lock.acquire(), timeout=3)
        except TimeoutError:
            raise HTTPException(503, "Natural TTS is busy") from None
        stop = threading.Event()
        self.cancel_events.add(stop)
        queue = asyncio.Queue(maxsize=6)
        loop = asyncio.get_running_loop()
        completed = asyncio.Event()
        sample_count = 0
        started = time.perf_counter()
        first = None

        def send(item):
            pending = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while not stop.is_set():
                try:
                    pending.result(timeout=0.2)
                    return True
                except TimeoutError:
                    continue
            pending.cancel()
            return False

        def producer():
            nonlocal sample_count, first
            generation = None
            tempo = None
            resolved_profile = profile or ("female" if voice == settings.natural_tts_female_voice else "male")

            def emit(audio):
                nonlocal sample_count, first
                if not audio.size:
                    return True
                if first is None:
                    first = time.perf_counter() - started
                pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                sample_count += len(audio)
                return send(pcm)

            try:
                if resolved_profile == "male" and settings.male_speech_rate != 1:
                    tempo = SpeechTempo(settings.male_speech_rate)
                generation = self.generate(text, voice, resolved_profile)
                for result in generation:
                    if stop.is_set():
                        break
                    audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                    if not np.isfinite(audio).all():
                        raise ValueError("Nonfinite TTS output")
                    if not audio.size:
                        continue
                    if tempo is not None:
                        audio = tempo.push(audio)
                    if not emit(audio):
                        break
                if not stop.is_set():
                    if tempo is not None:
                        emit(tempo.finish())
                    send(None)
            except Exception as exc:
                self.metrics["errors"] += 1
                failure("natural-tts", type(exc).__name__)
                event("natural_tts_generation_failed", error_type=type(exc).__name__)
                if not stop.is_set():
                    send(exc)
            finally:
                if generation is not None:
                    with contextlib.suppress(Exception):
                        generation.close()
                if tempo is not None:
                    tempo.close()
                loop.call_soon_threadsafe(completed.set)

        loop.run_in_executor(self.executor, producer)

        ended = False
        cleanup = None

        def finalize():
            nonlocal cleanup
            if cleanup is not None:
                return cleanup
            stop.set()

            async def release_slot():
                await completed.wait()
                self.cancel_events.discard(stop)
                if self.generation_lock.locked():
                    self.generation_lock.release()

            cleanup = asyncio.create_task(release_slot())
            self.cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self.cleanup_tasks.discard)
            elapsed = time.perf_counter() - started
            if ended:
                self.metrics["completed"] += 1
                if first is not None:
                    self.metrics["first_audio_seconds"] = (
                        self.metrics["first_audio_seconds"] + [round(first, 3)]
                    )[-100:]
                if sample_count:
                    self.metrics["rtf"] = (
                        self.metrics["rtf"] + [round(elapsed / (sample_count / 24000), 3)]
                    )[-100:]
            else:
                self.metrics["cancelled"] += 1
            event(
                "natural_tts_stream_finished",
                complete=ended,
                first_seconds=round(first, 3) if first is not None else None,
                audio_seconds=round(sample_count / 24000, 3),
                elapsed_seconds=round(elapsed, 3),
            )
            return cleanup

        async def body():
            nonlocal ended
            try:
                while True:
                    async with asyncio.timeout(30):
                        item = await queue.get()
                    if item is None:
                        ended = True
                        break
                    if isinstance(item, Exception):
                        raise RuntimeError("TTS stream failed") from None
                    yield item
            finally:
                cleanup_task = finalize()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup_task)

        return PCMResponse(
            body(),
            finalize=finalize,
            media_type="application/octet-stream",
            headers={
                "X-Audio-Sample-Rate": "24000",
                "X-Audio-Format": "pcm_s16le",
                "Cache-Control": "no-store",
            },
        )


engine = Engine()


@asynccontextmanager
async def lifespan(app):
    if len(settings.natural_tts_api_key) < 16:
        raise RuntimeError("NATURAL_TTS_API_KEY is required")
    task = asyncio.get_running_loop().run_in_executor(engine.executor, engine.load)
    yield
    for stop in engine.cancel_events:
        stop.set()
    await asyncio.gather(task, *engine.cleanup_tasks, return_exceptions=True)
    await asyncio.to_thread(engine.executor.shutdown, wait=True, cancel_futures=True)


app = FastAPI(title="Local natural TTS", lifespan=lifespan, docs_url=None, redoc_url=None)


def authorized(authorization: str = Header(default="")):
    if not secrets.compare_digest(
        authorization.encode(), ("Bearer " + settings.natural_tts_api_key).encode()
    ):
        raise HTTPException(401, "Unauthorized")


@app.get("/health", dependencies=[Depends(authorized)])
async def health():
    return {
        "ready": engine.ready,
        "backend": "qwen3-tts-mlx",
        "voice": settings.natural_tts_voice,
        "sample_rate": 24000,
        "error": engine.error,
        "busy": engine.generation_lock.locked(),
        "metrics": engine.metrics,
    }


@app.post("/synthesize", dependencies=[Depends(authorized)])
async def synthesize(data: Synthesis, request: Request):
    return await engine.stream(data.text, request, data.voice, data.profile)
