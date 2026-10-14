import asyncio
import threading
from types import SimpleNamespace

import pytest
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor

from app.config import settings
from app.dialogue import Dialogue, EventBus
from app.schemas import ExtractedIntent
from app.voice import (
    DialogueProcessor,
    LocalSTT,
    PreparedTranscriptionFrame,
    STTAudioActivityFrame,
    VoiceManager,
)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def service(monkeypatch, dialogue, runtime=None):
    monkeypatch.setattr(settings, "stt_preemptive_enabled", True)

    class Models:
        calls = 0

        def transcribe(self, audio):
            self.calls += 1
            return "  Хочу обсудить  необычный вопрос.\n" if self.calls == 1 else "Уточнение после паузы"

    manager = SimpleNamespace(runtime={"test": runtime if runtime is not None else {}}, dialogue=dialogue)
    stt = LocalSTT(Models(), EventBus(), "test", manager)
    manager.runtime["test"]["stt"] = stt
    stt._sample_rate = 16000
    stt._user_speaking = True
    stt._samples = 8000
    stt._audio_buffer.extend(b"\x01\x00" * 8000)
    return stt, manager


async def test_endpoint_publishes_final_text_without_waiting_for_preparation(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class Dialogue:
        calls = []

        async def prepare(self, sid, text):
            self.calls.append(text)
            started.set()
            await release.wait()

    dialogue = Dialogue()
    stt, _ = service(monkeypatch, dialogue)
    try:
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await asyncio.wait_for(started.wait(), 2)
        assert dialogue.calls == ["Хочу обсудить необычный вопрос."]
        assert stt._segments == [] or not stt._segments
        await stt._handle_user_stopped_speaking(VADUserStoppedSpeakingFrame())
        frames = await asyncio.wait_for(collect(stt.run_stt(b"")), 0.1)
        assert len(frames) == 1 and isinstance(frames[0], PreparedTranscriptionFrame)
        assert frames[0].text == dialogue.calls[0]
        assert frames[0].prepared is stt._prepared_task
        assert not frames[0].prepared.done()
        assert stt.models.calls == 1
    finally:
        release.set()
        await stt.cleanup()


async def collect(iterator):
    return [frame async for frame in iterator]


async def test_resumed_speech_cancels_stale_preparation_and_never_reuses_it(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Dialogue:
        calls = 0

        async def prepare(self, sid, text):
            self.calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    dialogue = Dialogue()
    stt, _ = service(monkeypatch, dialogue)
    try:
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await asyncio.wait_for(started.wait(), 2)
        stt._observe_activity(STTAudioActivityFrame(state=VADState.SPEAKING, samples=320))
        await asyncio.wait_for(cancelled.wait(), 2)
        stt._audio_buffer.extend(b"\x02\x00" * 800)
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await wait_until(lambda: stt.models.calls == 2)
        await stt._handle_user_stopped_speaking(VADUserStoppedSpeakingFrame())
        frames = await collect(stt.run_stt(b""))
        assert frames[0].text == "Уточнение после паузы"
        assert frames[0].prepared is None
        assert dialogue.calls == 1
    finally:
        await stt.cleanup()


async def test_pending_stt_finishing_after_endpoint_does_not_start_early_extraction(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Dialogue:
        calls = 0

        async def prepare(self, *args):
            self.calls += 1

    class Models:
        def transcribe(self, audio):
            entered.set()
            release.wait(2)
            return "Хочу обсудить необычный вопрос"

    dialogue = Dialogue()
    stt, _ = service(monkeypatch, dialogue)
    stt.models = Models()
    try:
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await wait_until(entered.is_set)
        await stt._handle_user_stopped_speaking(VADUserStoppedSpeakingFrame())
        pending = asyncio.create_task(collect(stt.run_stt(b"")))
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        frames = await asyncio.wait_for(pending, 2)
        assert frames[0].prepared is None
        assert dialogue.calls == 0
    finally:
        release.set()
        await stt.cleanup()


async def test_cancellation_resistant_preparation_is_bounded_and_cannot_be_reused(monkeypatch):
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Dialogue:
        calls = 0

        async def prepare(self, *args):
            self.calls += 1
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            return "Obsolete extraction"

    dialogue = Dialogue()
    stt, _ = service(monkeypatch, dialogue)
    try:
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await asyncio.wait_for(started.wait(), 2)
        obsolete = stt._prepared_task
        # A newer utterance must not launch another speculative model request
        # while a cancelled backend request has not actually stopped yet.
        await stt._handle_user_started_speaking(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(cancelled.wait(), 2)
        stt._samples = 8000
        stt._audio_buffer.extend(b"\x02\x00" * 8000)
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await wait_until(lambda: stt.models.calls == 2)
        await asyncio.sleep(0)
        assert dialogue.calls == 1
        await stt._handle_user_stopped_speaking(VADUserStoppedSpeakingFrame())
        frames = await collect(stt.run_stt(b""))
        assert frames[0].prepared is None
        release.set()
        assert await obsolete == "Obsolete extraction"
        assert frames[0].text == "Уточнение после паузы"
    finally:
        release.set()
        await stt.cleanup()


@pytest.mark.parametrize("gate", ["needs_consent", "human", "queued"])
async def test_runtime_gate_prevents_speculation(monkeypatch, gate):
    class Dialogue:
        calls = 0

        async def prepare(self, *args):
            self.calls += 1

    dialogue = Dialogue()
    stt, _ = service(monkeypatch, dialogue, {gate: True})
    try:
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await wait_until(lambda: stt._preemptive_task.done())
        await asyncio.sleep(0)
        assert dialogue.calls == 0
    finally:
        await stt.cleanup()


async def test_cleanup_cancels_preparation_and_joins_task(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Dialogue:
        async def prepare(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    stt, _ = service(monkeypatch, Dialogue())
    stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
    await asyncio.wait_for(started.wait(), 2)
    task = stt._prepared_task
    await stt.cleanup()
    assert task.cancelled() and cancelled.is_set()
    assert stt._prepared_task is stt._preemptive_task is None


async def test_cleanup_invalidates_already_completed_stt_before_its_callback(monkeypatch):
    class Dialogue:
        calls = 0

        async def prepare(self, *args):
            self.calls += 1

    dialogue = Dialogue()
    stt, _ = service(monkeypatch, dialogue)

    async def transcribed():
        return "Хочу поговорить"

    task = asyncio.create_task(transcribed())
    await task
    stt._preemptive_task = task
    stt._preemptive_valid = True
    stt._preemptive_utterance = stt._utterance
    task.add_done_callback(stt._early_transcribed)
    await stt.cleanup()
    assert dialogue.calls == 0
    assert stt._prepared_task is None


async def test_interruption_frame_invalidates_and_cancels_preparation(monkeypatch):
    started = asyncio.Event()

    class Dialogue:
        async def prepare(self, *args):
            started.set()
            await asyncio.Event().wait()

    stt, _ = service(monkeypatch, Dialogue())
    worker = PipelineWorker(Pipeline([stt]), params=PipelineParams(audio_in_sample_rate=16000))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    try:
        await asyncio.sleep(0.02)
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await asyncio.wait_for(started.wait(), 2)
        task = stt._prepared_task
        await worker.queue_frame(InterruptionFrame())
        await wait_until(task.cancelled)
        assert not stt._prepared_valid and not stt._preemptive_valid
    finally:
        await worker.queue_frame(CancelFrame())
        await asyncio.wait_for(runner, 3)


async def test_manager_interrupt_cancels_preparation_even_without_vad(monkeypatch):
    started = asyncio.Event()

    class Dialogue:
        async def prepare(self, *args):
            started.set()
            await asyncio.Event().wait()

        async def interrupt_playback(self, sid):
            pass

    stt, _ = service(monkeypatch, Dialogue())

    class Gate:
        async def broadcast_interruption(self):
            pass

    manager = VoiceManager(None, stt.models, stt.manager.dialogue, EventBus())
    manager.runtime = stt.manager.runtime
    manager.runtime["test"]["gate"] = Gate()
    try:
        stt._observe_activity(STTAudioActivityFrame(state=VADState.STOPPING, samples=2400))
        await asyncio.wait_for(started.wait(), 2)
        task = stt._prepared_task
        await manager.interrupt("test")
        await wait_until(task.cancelled)
        assert manager.runtime["test"]["generation"] == 1
    finally:
        await stt.cleanup()


async def test_initialized_pipeline_overlaps_pure_extraction_and_reuses_pending_result(business, session, monkeypatch):
    monkeypatch.setattr(settings, "stt_preemptive_enabled", True)
    started, release = asyncio.Event(), asyncio.Event()
    sid = session["uuid"]

    class Models:
        extractions = 0
        transcriptions = 0

        def transcribe(self, pcm):
            self.transcriptions += 1
            return "Хочу обсудить необычный вопрос"

        async def extract(self, *args):
            self.extractions += 1
            started.set()
            await release.wait()
            return ExtractedIntent(intent="conversation", answer="Давайте обсудим этот вопрос.")

    class Collector(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.texts, self.spoken = [], []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                self.texts.append(frame.text)
            if isinstance(frame, TTSSpeakFrame):
                self.spoken.append(frame.text)
            await self.push_frame(frame, direction)

    models, events = Models(), EventBus()
    dialogue = Dialogue(business, models, events)
    manager = VoiceManager(business, models, dialogue, events)
    stt = LocalSTT(models, events, sid, manager)
    manager.runtime[sid] = {"stt": stt, "generation": 0}
    before, after = Collector(), Collector()
    worker = PipelineWorker(Pipeline([stt, before, DialogueProcessor(manager, sid), after]), params=PipelineParams(audio_in_sample_rate=16000))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    try:
        await worker.queue_frames([
            VADUserStartedSpeakingFrame(), InputAudioRawFrame(b"\x01\x00" * 8000, 16000, 1),
            STTAudioActivityFrame(state=VADState.SPEAKING, samples=8000),
            InputAudioRawFrame(b"\x00\x00" * 2400, 16000, 1),
            STTAudioActivityFrame(state=VADState.STOPPING, samples=2400),
        ])
        await asyncio.wait_for(started.wait(), 2)
        assert before.texts == after.spoken == []
        assert await business.pool.fetchval("SELECT count(*) FROM turns") == 0
        await worker.queue_frame(VADUserStoppedSpeakingFrame())
        await wait_until(lambda: bool(before.texts))
        assert before.texts == ["Хочу обсудить необычный вопрос"]
        assert after.spoken == []
        release.set()
        await wait_until(lambda: bool(after.spoken))
        assert after.spoken == ["Давайте обсудим этот вопрос."]
        assert models.transcriptions == models.extractions == 1
        assert await business.pool.fetchval("SELECT count(*) FROM turns") == 2
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(runner, 3)
    finally:
        release.set()
        if not runner.done():
            await worker.queue_frame(CancelFrame())
            await asyncio.wait_for(runner, 3)
