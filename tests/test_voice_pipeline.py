import asyncio
from uuid import UUID

from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor

from app.dialogue import Dialogue, EventBus
from app.voice import LocalSTT, VoiceManager


async def test_long_speech_preserves_all_chunks_in_one_final_transcription():
    calls, results = [], []

    class Models:
        def transcribe(self, audio):
            calls.append(len(audio))
            return "Первая часть" if len(calls) == 1 else "Вторая часть"

    class Collector(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                results.append(frame.text)
            await self.push_frame(frame, direction)

    worker = PipelineWorker(Pipeline([LocalSTT(Models(), EventBus(), "test"), Collector()]), params=PipelineParams(audio_in_sample_rate=16000))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
    # System VAD frames have priority over audio in Pipecat; reproduce real
    # arrival order instead of enqueuing stop ahead of buffered audio at once.
    await worker.queue_frame(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.02)
    await worker.queue_frame(InputAudioRawFrame(b"\x01\x00" * (16000 * 22), 16000, 1))
    await asyncio.sleep(0.05)
    await worker.queue_frames([VADUserStoppedSpeakingFrame(), EndFrame()])
    await asyncio.wait_for(runner, timeout=5)
    assert results == ["Первая часть Вторая часть"]
    assert all(size <= 16000 * 2 * 21 for size in calls)


async def test_handoff_timeout_is_spoken_to_voice_caller(business):
    data = await business.new_session("voice", True)
    sid = UUID(data["id"])
    await business.pool.execute("UPDATE sessions SET state='handoff_pending' WHERE id=$1", sid)
    heard = asyncio.Event()
    frames = []

    class Worker:
        async def queue_frame(self, frame):
            frames.append(frame)
            heard.set()

    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.runtime[sid] = {"worker": Worker()}
    manager.request_handoff(sid, timeout_seconds=0.01)
    await asyncio.wait_for(heard.wait(), timeout=1)
    assert "не ответил" in frames[0].text
    assert await business.pool.fetchval("SELECT state FROM sessions WHERE id=$1", sid) == "bot"
