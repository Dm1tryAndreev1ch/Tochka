import asyncio
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams, VADState
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
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
from app.dialogue import EventBus
from app.voice import (
    DialogueProcessor,
    LocalSTT,
    LocalTTS,
    STTAudioActivityFrame,
    TurnVADAnalyzer,
    TurnVADProcessor,
)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class Collector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.results = []
        self.spoken = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self.results.append(frame.text)
        if isinstance(frame, TTSSpeakFrame):
            self.spoken.append(frame.text)
        await self.push_frame(frame, direction)


@pytest_asyncio.fixture
async def harness(monkeypatch):
    monkeypatch.setattr(settings, "stt_preemptive_enabled", True)
    workers, runners = [], []

    async def build(models, extra_processors=()):
        collector = Collector()
        worker = PipelineWorker(
            Pipeline([LocalSTT(models, EventBus(), "test"), collector, *extra_processors]),
            params=PipelineParams(audio_in_sample_rate=16000),
        )
        runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
        workers.append(worker)
        runners.append(runner)
        await worker.queue_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.02)
        return worker, runner, collector.results

    yield build
    for worker, runner in zip(workers, runners, strict=True):
        if not runner.done():
            await worker.queue_frame(CancelFrame())
        await asyncio.wait_for(runner, timeout=3)


async def audio(worker, state, seconds=0.5, marker=1):
    samples = int(16000 * seconds)
    await worker.queue_frames(
        [
            InputAudioRawFrame(bytes([marker, 0]) * samples, 16000, 1),
            STTAudioActivityFrame(state=state, samples=samples),
        ]
    )
    await asyncio.sleep(0.02)


async def finish(worker, runner):
    await worker.queue_frames([VADUserStoppedSpeakingFrame(), EndFrame()])
    await asyncio.wait_for(runner, timeout=3)


async def test_transcription_overlaps_endpoint_silence_without_publishing_partial(harness):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Models:
        def transcribe(self, pcm):
            calls.append(pcm)
            started.set()
            release.wait(2)
            return "Сколько стоит консультация?"

    worker, runner, results = await harness(Models())
    try:
        await audio(worker, VADState.SPEAKING)
        await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
        await wait_until(started.is_set)
        assert results == []
        release.set()
        await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
        await finish(worker, runner)
        assert results == ["Сколько стоит консультация?"]
        assert len(calls) == 1
    finally:
        release.set()


async def test_resumed_speech_invalidates_speculation_and_keeps_correction(harness):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Models:
        def transcribe(self, pcm):
            calls.append(pcm)
            if len(calls) == 1:
                started.set()
                release.wait(2)
                return "Разрешаю записывать"
            return "Разрешаю записывать, нет, не записывайте"

    worker, runner, results = await harness(Models())
    try:
        await audio(worker, VADState.SPEAKING)
        await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
        await wait_until(started.is_set)
        await audio(worker, VADState.SPEAKING, marker=2)
        release.set()
        await finish(worker, runner)
        assert results == ["Разрешаю записывать, нет, не записывайте"]
        assert len(calls) == 2
        assert b"\x02\x00" in calls[1]
    finally:
        release.set()


async def test_a_new_utterance_drops_the_old_inflight_transcription(harness):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Models:
        def transcribe(self, pcm):
            calls.append(pcm)
            if len(calls) == 1:
                started.set()
                release.wait(2)
                return "Да, подтверждаю"
            return "Нет, отмена"

    worker, runner, results = await harness(Models())
    try:
        await audio(worker, VADState.SPEAKING)
        await worker.queue_frame(VADUserStoppedSpeakingFrame())
        await wait_until(started.is_set)
        await worker.queue_frame(VADUserStartedSpeakingFrame())
        await audio(worker, VADState.SPEAKING, marker=2)
        release.set()
        await finish(worker, runner)
        assert results == ["Нет, отмена"]
        assert len(calls) == 2
    finally:
        release.set()


async def test_speculative_work_is_bounded_across_successive_utterances(harness):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Models:
        def transcribe(self, pcm):
            calls.append(pcm)
            if len(calls) == 1:
                started.set()
                release.wait(2)
                return "Первый вопрос"
            return "Второй вопрос"

    worker, runner, results = await harness(Models())
    try:
        await audio(worker, VADState.SPEAKING)
        await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
        await wait_until(started.is_set)
        await worker.queue_frame(VADUserStoppedSpeakingFrame())
        await worker.queue_frame(VADUserStartedSpeakingFrame())
        await audio(worker, VADState.SPEAKING, marker=2)
        await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
        assert len(calls) == 1
        release.set()
        await finish(worker, runner)
        assert results == ["Второй вопрос"]
        assert len(calls) == 2
    finally:
        release.set()


async def test_failed_speculation_retries_complete_audio(harness):
    calls = []

    class Models:
        def transcribe(self, pcm):
            calls.append(pcm)
            if len(calls) == 1:
                raise RuntimeError("temporary STT failure")
            return "Какой у вас адрес?"

    worker, runner, results = await harness(Models())
    await audio(worker, VADState.SPEAKING)
    await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
    await wait_until(lambda: len(calls) == 1)
    await finish(worker, runner)
    assert results == ["Какой у вас адрес?"]
    assert len(calls) == 2


async def test_cancelled_pipeline_never_delivers_speculative_transcript(harness):
    started, release, ended = threading.Event(), threading.Event(), threading.Event()

    class Models:
        def transcribe(self, pcm):
            started.set()
            release.wait(2)
            ended.set()
            return "Да, подтверждаю"

    worker, runner, results = await harness(Models())
    try:
        await audio(worker, VADState.SPEAKING)
        await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
        await wait_until(started.is_set)
        await worker.queue_frame(CancelFrame())
        await asyncio.wait_for(runner, timeout=3)
        release.set()
        await wait_until(ended.is_set)
        assert results == []
    finally:
        release.set()


async def test_early_transcript_cannot_grant_consent_or_run_dialogue(harness):
    started = threading.Event()
    consent_checks, messages = [], []

    class Models:
        def transcribe(self, pcm):
            started.set()
            return "Согласен"

    class Manager:
        runtime = {"test": {"needs_consent": True, "human": False}}

        async def handle_consent(self, sid, text):
            consent_checks.append(text)
            return True

        class dialogue:
            @staticmethod
            async def message(*args):
                messages.append(args)

    worker, runner, results = await harness(Models(), [DialogueProcessor(Manager(), "test")])
    await audio(worker, VADState.SPEAKING)
    await audio(worker, VADState.STOPPING, seconds=0.15, marker=0)
    await wait_until(started.is_set)
    assert consent_checks == messages == results == []
    await finish(worker, runner)
    assert consent_checks == ["Согласен"]
    assert messages == []


async def test_vad_keeps_longer_pauses_for_sensitive_fields_and_does_not_reset_speech(monkeypatch):
    monkeypatch.setattr(settings, "vad_stop_seconds", 0.45)
    monkeypatch.setattr(settings, "vad_sensitive_stop_seconds", 0.95)
    runtime = {}
    states = iter([VADState.SPEAKING, VADState.SPEAKING, VADState.QUIET, VADState.STARTING])

    async def analyze(self, pcm):
        return next(states)

    monkeypatch.setattr(SileroVADAnalyzer, "analyze_audio", analyze)
    analyzer = TurnVADAnalyzer(SimpleNamespace(runtime={"test": runtime}), "test", params=VADParams())
    analyzer.set_sample_rate(16000)
    try:
        await analyzer.analyze_audio(b"\x00\x00")
        assert analyzer.params.stop_secs == settings.vad_stop_seconds
        runtime["sensitive_input"] = True
        await analyzer.analyze_audio(b"\x00\x00")
        assert analyzer.params.stop_secs == settings.vad_stop_seconds
        await analyzer.analyze_audio(b"\x00\x00")
        await analyzer.analyze_audio(b"\x00\x00")
        assert analyzer.params.stop_secs == settings.vad_sensitive_stop_seconds
    finally:
        await analyzer.cleanup()


async def test_vad_and_stt_pipeline_overlap_inference_and_emit_only_after_endpoint(monkeypatch):
    """Use the production processor ordering, including priority VAD frames."""
    monkeypatch.setattr(settings, "stt_preemptive_enabled", True)
    started = threading.Event()
    calls = []

    class Analyzer(VADAnalyzer):
        state = VADState.QUIET
        quiet_samples = 0

        def num_frames_required(self):
            return 320

        def voice_confidence(self, pcm):
            return 1.0 if pcm[0] else 0.0

        async def analyze_audio(self, pcm):
            if pcm[0]:
                self.quiet_samples = 0
                self.state = VADState.SPEAKING
            else:
                self.quiet_samples += len(pcm) // 2
                self.state = VADState.QUIET if self.quiet_samples >= self.sample_rate * self.params.stop_secs else VADState.STOPPING
            return self.state

    class Models:
        def transcribe(self, pcm):
            calls.append(pcm)
            started.set()
            return "Когда вы открываетесь?"

    collector = Collector()
    worker = PipelineWorker(
        Pipeline(
            [
                TurnVADProcessor(vad_analyzer=Analyzer(params=VADParams(stop_secs=0.45))),
                LocalSTT(Models(), EventBus(), "test"),
                collector,
            ]
        ),
        params=PipelineParams(audio_in_sample_rate=16000),
    )
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))

    async def feed(seconds, marker):
        await worker.queue_frame(InputAudioRawFrame(bytes([marker, 0]) * int(16000 * seconds), 16000, 1))
        await asyncio.sleep(0.02)

    try:
        await feed(0.2, 1)
        await feed(0.5, 1)
        await feed(0.15, 0)
        await wait_until(started.is_set)
        assert collector.results == []
        await feed(0.35, 0)
        await wait_until(lambda: bool(collector.results))
        assert collector.results == ["Когда вы открываетесь?"]
        assert len(calls) == 1
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(runner, timeout=3)
    finally:
        if not runner.done():
            await worker.queue_frame(CancelFrame())
            await asyncio.wait_for(runner, timeout=3)


@pytest.mark.parametrize(
    "text,runtime,expected",
    [
        ("Как к вам обращаться?", {}, True),
        ("Вы записаны, Иван, ваш номер +375291234567.", {}, False),
        ("Ваши часы работы — с девяти до шести.", {"cacheable_text": "Ваши часы работы — с девяти до шести."}, True),
        ("Здравствуйте. Чем могу помочь?", {"cacheable_text": "Здравствуйте. Чем могу помочь?"}, True),
    ],
)
async def test_only_public_speech_is_opted_into_tts_cache(text, runtime, expected):
    calls = []

    class Models:
        async def stream_speech(self, text, profile, *, cache=False):
            calls.append((text, profile, cache))
            yield b"\x00\x00" * 240

    service = LocalTTS(Models(), manager=SimpleNamespace(runtime={"test": runtime}), sid="test")
    service._sample_rate = 24000
    frames = [frame async for frame in service.run_tts(text, "ctx")]
    assert frames
    assert calls and all(call[2] is expected for call in calls)


@pytest.mark.parametrize(
    "reply,cacheable,sensitive",
    [
        ({"text": "Как к вам обращаться?", "collected": {"intent": "booking"}}, True, False),
        ({"text": "На какую дату хотите записаться?", "collected": {}}, True, True),
        ({"text": "Назовите телефон с кодом страны или введите его текстом.", "collected": {}}, True, True),
        ({"text": "Наш офис работает до шести.", "response_source": "knowledge"}, True, False),
        ({"text": "Проверьте запись для Ивана на завтра.", "draft": {"id": "draft-1"}}, False, True),
        ({"text": "Ваш номер +375291234567 уже записан.", "collected": {}}, False, False),
    ],
)
async def test_dialogue_marks_only_public_replies_cacheable_and_sensitive_prompts(reply, cacheable, sensitive):
    runtime = {"generation": 0}

    class Manager:
        def __init__(self):
            self.runtime = {"test": runtime}

        async def handle_consent(self, sid, text):
            return False

        class dialogue:
            @staticmethod
            async def message(*args):
                return reply

    collector = Collector()
    worker = PipelineWorker(Pipeline([DialogueProcessor(Manager(), "test"), collector]))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    await worker.queue_frames([TranscriptionFrame("Вопрос", "test", "now"), EndFrame()])
    await asyncio.wait_for(runner, timeout=3)
    assert collector.spoken == [reply["text"]]
    assert runtime["cacheable_text"] == (reply["text"] if cacheable else None)
    assert runtime["sensitive_input"] is sensitive


async def test_interrupted_dialogue_generation_is_never_sent_to_tts():
    entered, release = asyncio.Event(), asyncio.Event()
    runtime = {"generation": 0}
    interrupted = []

    class Manager:
        def __init__(self):
            self.runtime = {"test": runtime}

        async def handle_consent(self, sid, text):
            return False

        class dialogue:
            @staticmethod
            async def message(*args):
                entered.set()
                await release.wait()
                return {"text": "Прежний ответ", "utterance_id": "old"}

            @staticmethod
            async def interrupt_playback(sid):
                interrupted.append(sid)

    collector = Collector()
    worker = PipelineWorker(Pipeline([DialogueProcessor(Manager(), "test"), collector]))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    try:
        await worker.queue_frame(TranscriptionFrame("Первый вопрос", "test", "now"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        runtime["generation"] = 1
        release.set()
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(runner, timeout=3)
        assert collector.spoken == []
        assert interrupted == ["test"]
    finally:
        release.set()
        if not runner.done():
            await worker.queue_frame(CancelFrame())
            await asyncio.wait_for(runner, timeout=3)
