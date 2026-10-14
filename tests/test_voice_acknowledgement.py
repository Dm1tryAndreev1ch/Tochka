import asyncio
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from app.config import settings
from app.voice import (
    AcknowledgementAudioFrame,
    DelayedAcknowledgement,
    DeliveryTracker,
    DialogueProcessor,
    SpeechChunkSent,
)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.002)


@pytest.fixture
def setup_ack(monkeypatch):
    monkeypatch.setattr(settings, "voice_acknowledgement_enabled", True)
    monkeypatch.setattr(settings, "voice_acknowledgement_delay_seconds", 0.035)
    frames = []

    async def queue(frame):
        frames.append(frame)

    runtime = {"generation": 0, "profile": "female", "client_output": SimpleNamespace(queue_frame=queue), "speech_end": time.perf_counter()}
    cached = []

    def getter(profile):
        cached.append(profile)
        return b"\x10\x00" * 24000

    async def emit(*_):
        pass

    manager = SimpleNamespace(runtime={"test": runtime}, models=SimpleNamespace(cached_acknowledgement=getter), events=SimpleNamespace(emit=emit))
    return manager, runtime, frames, cached


async def test_delayed_once_and_cancels_without_waiting_for_clip_tail(setup_ack):
    manager, runtime, frames, cached = setup_ack
    ack = DelayedAcknowledgement(manager, "test", runtime)
    ack.start()
    await asyncio.sleep(0.012)
    assert frames == []
    await until(lambda: bool(frames))
    started = time.perf_counter()
    await ack.stop()
    assert time.perf_counter() - started < 0.1
    assert cached == ["female"]
    assert sum(isinstance(f, AcknowledgementAudioFrame) for f in frames) <= 2
    assert isinstance(frames[-1], InterruptionFrame)
    assert "acknowledgement" not in runtime
    count = len(frames)
    await asyncio.sleep(0.05)
    assert len(frames) == count


async def test_quick_answer_and_cache_miss_never_send_audio(setup_ack):
    manager, runtime, frames, _ = setup_ack
    ack = DelayedAcknowledgement(manager, "test", runtime)
    ack.start()
    await ack.stop()
    assert frames == []
    manager.models.cached_acknowledgement = lambda _: None
    ack = DelayedAcknowledgement(manager, "test", runtime)
    ack.start()
    assert ack.task is None
    await ack.stop()
    assert frames == []


@pytest.mark.parametrize("state", [
    {"needs_consent": True}, {"consent_rejected": True}, {"human": True}, {"queued": True},
    {"sensitive_input": True}, {"acknowledgement_sensitive": True},
    {"recording_failure_task": object()}, {"policy": {"record_audio": True}},
])
async def test_private_or_unavailable_session_never_reads_cache_or_speaks(setup_ack, state):
    manager, runtime, frames, cached = setup_ack
    runtime.update(state)
    ack = DelayedAcknowledgement(manager, "test", runtime)
    ack.start()
    await ack.stop()
    assert frames == cached == []


@pytest.mark.parametrize("change", ["consent", "recording", "end", "interrupt"])
async def test_changed_permission_or_ended_turn_stops_pcm(setup_ack, change):
    manager, runtime, frames, _ = setup_ack
    ack = DelayedAcknowledgement(manager, "test", runtime)
    ack.start()
    await until(lambda: bool(frames))
    if change == "consent":
        runtime["needs_consent"] = True
    elif change == "recording":
        runtime["policy"] = {"record_audio": True}
    elif change == "end":
        manager.runtime.clear()
    else:
        runtime["generation"] += 1
        ack.cancel()
    count = sum(isinstance(f, AcknowledgementAudioFrame) for f in frames)
    await asyncio.sleep(0.06)
    await ack.stop()
    assert sum(isinstance(f, AcknowledgementAudioFrame) for f in frames) == count
    if change in {"end", "interrupt"}:
        assert not any(isinstance(f, InterruptionFrame) for f in frames), "old turn must not clear newer output"


@pytest.mark.parametrize("personal_text", ["Мой номер +375291234567", "Меня зовут Анна", "anna@example.com"])
async def test_personal_transcription_does_not_start_acknowledgement(setup_ack, personal_text):
    manager, runtime, frames, cached = setup_ack

    async def consent(*_):
        return False

    async def message(*_):
        await asyncio.sleep(0.07)
        return {"text": "Спасибо.", "collected": {"intent": "callback"}}

    manager.handle_consent = consent
    manager.dialogue = SimpleNamespace(message=message)
    class Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    worker = PipelineWorker(Pipeline([DialogueProcessor(manager, "test"), Passthrough()]))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    await worker.queue_frames([TranscriptionFrame(personal_text, "test", "now"), EndFrame()])
    await asyncio.wait_for(runner, 3)
    assert frames == cached == []
    assert runtime["acknowledgement_sensitive"]


async def test_cancelled_pipeline_stops_pending_dialogue_and_ack_task(setup_ack):
    manager, runtime, frames, _ = setup_ack
    entered = asyncio.Event()

    async def consent(*_):
        return False

    async def message(*_):
        entered.set()
        await asyncio.Event().wait()

    class Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    manager.handle_consent = consent
    manager.dialogue = SimpleNamespace(message=message)
    worker = PipelineWorker(Pipeline([DialogueProcessor(manager, "test"), Passthrough()]))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    await worker.queue_frame(TranscriptionFrame("Расскажи об этом", "test", "now"))
    await until(lambda: entered.is_set() and bool(frames))
    ack = runtime["acknowledgement"]
    await worker.queue_frame(CancelFrame())
    await asyncio.wait_for(runner, 3)
    assert ack.task.done()
    assert "acknowledgement" not in runtime
    count = len(frames)
    await asyncio.sleep(0.04)
    assert len(frames) == count


async def test_real_transport_ack_stop_keeps_useful_audio_and_exact_playback_history(monkeypatch):
    """Exercise Pipecat's actual priority queues, output chunking and cancellation."""
    monkeypatch.setattr(settings, "voice_acknowledgement_enabled", True)
    monkeypatch.setattr(settings, "voice_acknowledgement_delay_seconds", 0.01)
    release, entered = asyncio.Event(), asyncio.Event()
    written, events, receipts, interrupted = [], [], [], []
    utterance = str(uuid4())
    runtime = {"generation": 0, "speech_end": time.perf_counter(), "profile": "male"}

    class Output(BaseOutputTransport):
        async def start(self, frame):
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def write_audio_frame(self, frame):
            written.append(frame)
            await asyncio.sleep(0.02)
            return True

    class Speech(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSSpeakFrame):
                await self.push_frame(TTSAudioRawFrame(b"\x30\x00" * 2400, 24000, 1))
                await self.push_frame(TTSStoppedFrame())
                await self.push_frame(SpeechChunkSent(utterance_id=utterance, played_chars=len(frame.text), expected_seconds=0.1))
            else:
                await self.push_frame(frame, direction)

    async def consent(*_):
        return False

    async def message(*_):
        entered.set()
        await release.wait()
        return {"text": "Полезный ответ.", "utterance_id": utterance}

    async def emit(sid, payload):
        events.append(payload)

    async def audio_sent(*args):
        receipts.append(args)

    async def interrupt_playback(*args):
        interrupted.append(args)

    output = Output(TransportParams(audio_out_enabled=True, audio_out_sample_rate=24000))
    runtime["client_output"] = output
    manager = SimpleNamespace(
        runtime={"test": runtime}, handle_consent=consent,
        models=SimpleNamespace(cached_acknowledgement=lambda _: b"\x10\x00" * 24000),
        events=SimpleNamespace(emit=emit),
        dialogue=SimpleNamespace(message=message, audio_sent=audio_sent, interrupt_playback=interrupt_playback),
    )
    worker = PipelineWorker(Pipeline([DialogueProcessor(manager, "test"), Speech(), output, DeliveryTracker(manager, "test")]), params=PipelineParams(audio_out_sample_rate=24000))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    try:
        await worker.queue_frame(TranscriptionFrame("Объясни сложную тему", "test", "now"))
        await until(lambda: any(e.get("kind") == "acknowledgement" for e in events))
        released = time.perf_counter()
        release.set()
        await until(lambda: any(type(f) is TTSAudioRawFrame for f in written))
        assert time.perf_counter() - released < 0.2, "ack tail must not delay useful audio"
        await until(lambda: bool(receipts))
        assert receipts == [("test", UUID(utterance), len("Полезный ответ."))]
        assert interrupted == []
        assert len([e for e in events if e["type"] == "acknowledgement"]) == 1
        assert [e["kind"] for e in events if e["type"] == "voice_audio"] == ["acknowledgement", "response"]
        assert len([e for e in events if e["type"] == "playback_marker"]) == 1
        stages = [e for e in events if e["type"] == "voice_stage"]
        assert [e["stage"] for e in stages] == ["dialogue_started", "dialogue_done", "first_response_transport_pcm"]
        assert all(e["generation"] == 0 and "text" not in e for e in stages)
        assert all(e["utterance_id"] == utterance for e in stages[1:])
        assert [e["server_perf_seconds"] for e in stages] == sorted(e["server_perf_seconds"] for e in stages)
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(runner, 3)
    finally:
        release.set()
        if not runner.done():
            await worker.queue_frame(CancelFrame())
            await asyncio.wait_for(runner, 3)
