import asyncio
import time
from types import SimpleNamespace

from app.voice import LocalSTT, LocalTTS, PreparedTranscriptionFrame


def capture():
    events = []

    async def emit(sid, payload):
        events.append(payload)

    return events, SimpleNamespace(emit=emit)


async def test_prepared_stages_keep_original_turn_identity_without_transcript():
    events, bus = capture()
    started, release = asyncio.Event(), asyncio.Event()

    async def prepare(sid, text):
        started.set()
        await release.wait()
        return object()

    runtime = {"generation": 3, "utterance_id": "old-reply"}
    manager = SimpleNamespace(runtime={"test": runtime}, dialogue=SimpleNamespace(prepare=prepare))
    stt = LocalSTT(SimpleNamespace(), bus, "test", manager)
    task = asyncio.create_task(stt._prepare_timed("Личное имя и номер", runtime, 7, 3))
    await started.wait()
    runtime.update(generation=4, utterance_id="new-reply")
    release.set()
    await task
    assert [e["stage"] for e in events] == ["prepare_started", "prepare_ready"]
    assert all(e["generation"] == 3 and e["stt_utterance"] == 7 and e["utterance_id"] is None for e in events)
    assert all("text" not in e and "Личное" not in str(e) for e in events)
    assert events[1]["server_perf_seconds"] >= events[0]["server_perf_seconds"]


async def test_cancelled_preparation_never_reports_ready():
    events, bus = capture()
    entered = asyncio.Event()

    async def prepare(*_):
        entered.set()
        await asyncio.Event().wait()

    runtime = {"generation": 3}
    manager = SimpleNamespace(runtime={"test": runtime}, dialogue=SimpleNamespace(prepare=prepare))
    stt = LocalSTT(SimpleNamespace(), bus, "test", manager)
    task = asyncio.create_task(stt._prepare_timed("Вопрос", runtime, 7, 3))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [e["stage"] for e in events] == ["prepare_started", "prepare_cancelled"]


async def test_final_stt_event_has_identity_and_no_transcript():
    events, bus = capture()
    runtime = {"generation": 9}
    manager = SimpleNamespace(runtime={"test": runtime})
    stt = LocalSTT(SimpleNamespace(transcribe=lambda _: "Меня зовут Иван"), bus, "test", manager)
    stt._utterance = 4
    stt._segments.append((4, True, None))
    frames = [frame async for frame in stt.run_stt(b"\x00\x00")]
    assert isinstance(frames[0], PreparedTranscriptionFrame)
    assert frames[0].stt_utterance == runtime["stt_utterance"] == 4
    assert len(events) == 1
    assert events[0]["stage"] == "stt_final_ready"
    assert events[0]["generation"] == 9
    assert "text" not in events[0] and "Иван" not in str(events[0])


async def test_tts_stages_measure_useful_pcm_once_on_same_server_clock():
    events, bus = capture()
    metrics = []

    async def stream_speech(*args, **kwargs):
        yield b"\x01\x00" * 240
        yield b"\x01\x00" * 240

    models = SimpleNamespace(stream_speech=stream_speech, _metric=lambda *args: metrics.append(args))
    runtime = {"generation": 9, "stt_utterance": 4, "utterance_id": "reply-1", "speech_end": time.perf_counter() - 1}
    manager = SimpleNamespace(runtime={"test": runtime}, events=bus)
    tts = LocalTTS(models, manager=manager, sid="test")
    tts._sample_rate = 24000
    frames = [frame async for frame in tts.run_tts("Полезный ответ.", "ctx")]
    assert frames
    stages = [e for e in events if e["type"] == "voice_stage"]
    assert [e["stage"] for e in stages] == ["tts_requested", "first_useful_server_pcm"]
    assert all(e["generation"] == 9 and e["stt_utterance"] == 4 and e["utterance_id"] == "reply-1" for e in stages)
    assert stages[1]["server_perf_seconds"] >= stages[0]["server_perf_seconds"]
    assert len(metrics) == 1 and metrics[0][0] == "turn_first_audio_seconds"
    assert len([e for e in events if e["type"] == "turn_latency" and e["kind"] == "response"]) == 1
