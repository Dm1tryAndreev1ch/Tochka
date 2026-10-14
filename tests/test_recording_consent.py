from uuid import UUID

from app.archive import ConversationArchive
from app.dialogue import Dialogue, EventBus
from app.voice import VoiceManager


class Worker:
    async def queue_frame(self, frame):
        pass

    async def queue_frames(self, frames):
        pass


async def test_audio_recording_requires_oral_permission_after_notice(business):
    data = await business.new_transport_session("sip", "male")
    sid = UUID(data["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.archive = ConversationArchive(business.pool)
    runtime = {"needs_consent": True, "worker": Worker(), "policy": {"record_audio": True, "policy_version": 2}, "speech_after_notice": False}
    manager.runtime[sid] = runtime
    await manager.handle_consent(sid, "разрешаю")
    assert not await business.pool.fetchval("SELECT audio_consent FROM sessions WHERE id=$1", sid)
    assert not manager.archive.store.path(sid, "capture").exists()
    runtime["speech_after_notice"] = True
    await manager.handle_consent(sid, "разрешаю запись разговора")
    assert await business.pool.fetchval("SELECT audio_consent FROM sessions WHERE id=$1", sid)
    assert await business.pool.fetchval("SELECT consent_method FROM sessions WHERE id=$1", sid) == "oral"
    runtime["capture"].append("caller", b"\xe8\x03" * 16000, 16000)
    await business.end(sid)
    await manager.archive.finalize(sid, runtime["capture"])
    assert await business.pool.fetchval("SELECT audio_available FROM archive_entries WHERE id=$1", sid)
    assert (await manager.archive.audio(sid)).startswith(b"RIFF")


async def test_spoken_refusal_never_starts_recording(business):
    data = await business.new_transport_session("sip", "male")
    sid = UUID(data["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.archive = ConversationArchive(business.pool)
    manager.runtime[sid] = {"needs_consent": True, "worker": Worker(), "policy": {"record_audio": True}, "speech_after_notice": True}
    await manager.handle_consent(sid, "я не разрешаю запись разговора")
    assert not await business.pool.fetchval("SELECT audio_consent FROM sessions WHERE id=$1", sid)
    assert not manager.archive.store.path(sid, "capture").exists()


async def test_text_consent_is_not_accepted_before_completed_notice(business):
    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.runtime[sid] = {"needs_consent": True, "worker": Worker(), "policy": {"record_audio": False}, "speech_after_notice": False}
    await manager.handle_consent(sid, "разрешаю")
    assert not await business.pool.fetchval("SELECT consent FROM sessions WHERE id=$1", sid)
    manager.runtime[sid]["speech_after_notice"] = True
    await manager.handle_consent(sid, "разрешаю")
    row = await business.pool.fetchrow("SELECT consent,audio_consent,consent_method FROM sessions WHERE id=$1", sid)
    assert row["consent"] and row["consent_method"] == "oral"
    assert not row["audio_consent"]


async def test_staff_voice_is_preserved_in_the_recorded_dialogue(business):
    import io
    import wave

    import numpy as np
    from pipecat.frames.frames import InputAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection

    from app.voice import VoiceGate

    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.archive = ConversationArchive(business.pool)
    await business.pool.execute("UPDATE sessions SET consent=true,audio_consent=true WHERE id=$1", sid)
    capture = manager.archive.start_capture(sid)
    manager.runtime[sid] = {"human": True, "capture": capture, "client_output": Worker()}
    gate = VoiceGate(manager, sid, operator=True)
    await gate.process_frame(InputAudioRawFrame(b"\xe8\x03" * 1600, 16000, 1), FrameDirection.DOWNSTREAM)
    await business.end(sid)
    await manager.archive.finalize(sid, capture)
    with wave.open(io.BytesIO(await manager.archive.audio(sid))) as recording:
        audio = np.frombuffer(recording.readframes(recording.getnframes()), dtype=np.int16).reshape(-1, 2)
    assert np.any(audio[:, 1] != 0)


async def test_storage_failure_does_not_claim_recording_started(business, monkeypatch):
    from pipecat.frames.frames import EndFrame

    frames = []

    class RecordingWorker(Worker):
        async def queue_frames(self, values):
            frames.extend(values)

    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.archive = ConversationArchive(business.pool)

    def storage_failure(_sid):
        raise OSError("test storage failure")

    monkeypatch.setattr(manager.archive, "start_capture", storage_failure)
    manager.runtime[sid] = {"needs_consent": True, "worker": RecordingWorker(), "policy": {"record_audio": True}, "speech_after_notice": True}
    await manager.handle_consent(sid, "разрешаю запись разговора")
    row = await business.pool.fetchrow("SELECT consent,audio_consent FROM sessions WHERE id=$1", sid)
    assert not row["consent"] and not row["audio_consent"]
    assert any(isinstance(frame, EndFrame) for frame in frames)
    assert not manager.archive.store.path(sid, "capture").exists()


async def test_dtmf_handoff_cannot_bypass_disabled_agent_action(business):
    frames = []

    class RecordingWorker(Worker):
        async def queue_frame(self, frame):
            frames.append(frame)

    sid = UUID((await business.new_session("voice", True))["id"])
    await business.pool.execute("UPDATE settings SET value='{\"available\":true}' WHERE key='operator'")
    await business.pool.execute("UPDATE sessions SET agent_config=jsonb_set(agent_config,'{allowed_actions}','[]') WHERE id=$1", sid)
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.runtime[sid] = {"needs_consent": False, "worker": RecordingWorker()}
    await manager.dtmf(sid, "0")
    assert await business.pool.fetchval("SELECT state FROM sessions WHERE id=$1", sid) == "bot"
    assert "выключено" in frames[0].text
    await manager.close()


async def test_mid_call_storage_failure_ends_the_call_instead_of_silent_recording_loss(business, monkeypatch):
    import asyncio

    from pipecat.frames.frames import EndFrame

    frames = []

    class RecordingWorker(Worker):
        async def queue_frames(self, values):
            frames.extend(values)

    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.archive = ConversationArchive(business.pool)
    capture = manager.archive.start_capture(sid)

    def storage_failure(*args):
        raise OSError("test mid-call storage failure")

    monkeypatch.setattr(capture, "append", storage_failure)
    manager.runtime[sid] = {"worker": RecordingWorker(), "capture": capture}
    manager.capture_audio(sid, "caller", b"\xe8\x03" * 1600, 16000, 1)
    for _ in range(10):
        if frames:
            break
        await asyncio.sleep(0)
    assert any(isinstance(frame, EndFrame) for frame in frames)


async def test_consent_deadline_starts_after_the_completed_notice(business, monkeypatch):
    import asyncio

    from pipecat.frames.frames import EndFrame
    from pipecat.processors.frame_processor import FrameDirection

    from app.voice import DeliveryTracker, NoticeSent

    frames = []

    class RecordingWorker(Worker):
        async def queue_frame(self, frame):
            frames.append(frame)

        async def queue_frames(self, values):
            frames.extend(values)

    real_sleep = asyncio.sleep

    async def elapsed_sleep(seconds):
        if seconds == 45:
            return
        await real_sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", elapsed_sleep)
    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    events = EventBus()
    manager = VoiceManager(business, None, Dialogue(business, None, events), events)
    manager.queue.join(sid)
    manager.runtime[sid] = {"connected": True, "needs_consent": True, "profile": "male", "policy": {"record_audio": True}, "worker": RecordingWorker()}
    try:
        await manager.activate(sid)
        await real_sleep(0.01)
        assert not any(isinstance(frame, EndFrame) for frame in frames)
        tracker = DeliveryTracker(manager, sid)
        await tracker.process_frame(NoticeSent(), FrameDirection.DOWNSTREAM)
        await real_sleep(0.01)
        assert any(isinstance(frame, EndFrame) for frame in frames)
    finally:
        await manager.close()
