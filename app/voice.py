"""Local WebRTC audio pipeline and a two-peer operator bridge."""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from uuid import uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.config import settings
from app.errors import AppError
from app.speech import segments
from app.telemetry import event

# The local profile deliberately advertises a single interface. Otherwise VPN
# and Docker interfaces can win ICE priority on a laptop with multiple networks.
if settings.ice_host_ip:
    import ipaddress

    import aioice.ice

    ice_hosts = [ip.strip() for ip in settings.ice_host_ip.split(",")]
    for ip in ice_hosts:
        ipaddress.ip_address(ip)
    aioice.ice.get_host_addresses = lambda use_ipv4, use_ipv6: ice_hosts

logger.remove()
logger.add(lambda msg: logging.getLogger("pipecat").info(str(msg).rstrip()), level="WARNING")
log = logging.getLogger("voice.runtime")


class LocalSTT(SegmentedSTTService):
    def __init__(self, models, events, sid):
        super().__init__(
            sample_rate=16000,
            audio_passthrough=False,
            trailing_silence_secs=0.3,
            settings=STTSettings(model="whisper-small", language=Language.RU),
            ttfs_p99_latency=8.0,
        )
        self.models, self.events, self.sid = models, events, sid
        self._segment_queue = asyncio.Queue(maxsize=2)

    @property
    def wants_wav_segments(self):
        return False

    async def process_audio_frame(self, frame, direction):
        await super().process_audio_frame(frame, direction)
        if len(self._audio_buffer) > 16000 * 2 * 20:
            self._audio_buffer.clear()
            await self.events.emit(
                self.sid,
                {
                    "type": "error",
                    "message": "Слишком длинная реплика. Говорите короткими фразами до 20 секунд.",
                },
            )

    async def _handle_user_stopped_speaking(self, frame):
        if self._segment_queue.full():
            self._audio_buffer.clear()
            self._user_speaking = False
            await self.events.emit(
                self.sid, {"type": "error", "message": "Не успел обработать реплику. Повторите после ответа."}
            )
            return
        await super()._handle_user_stopped_speaking(frame)

    async def run_stt(self, audio):
        text = await asyncio.to_thread(self.models.transcribe, audio)
        if text:
            yield TranscriptionFrame(text, str(self.sid), datetime.now(UTC).isoformat())


class LocalTTS(TTSService):
    def __init__(self, models, voice_profile="male", manager=None, sid=None):
        super().__init__(
            sample_rate=24000,
            push_start_frame=True,
            push_stop_frames=False,
            settings=TTSSettings(model="piper", voice="ru_RU-denis-medium", language=Language.RU),
        )
        self.models = models
        self.voice_profile = voice_profile
        self.manager = manager
        self.sid = sid

    async def run_tts(self, text, context_id):
        first = True

        async def chunks():
            # Synthesize a sentence at a time to keep first audio ahead of long replies.
            for part in segments(text):
                async for audio in self.models.stream_speech(part, self.voice_profile):
                    yield audio

        async for frame in self._stream_audio_frames_from_iterator(
            chunks(), in_sample_rate=24000, context_id=context_id
        ):
            if first and self.manager:
                runtime = self.manager.runtime.get(self.sid)
                end = runtime.pop("speech_end", None) if runtime else None
                if end is not None:
                    elapsed = time.perf_counter() - end
                    self.models._metric("turn_first_audio_seconds", elapsed)
                    event("turn_first_audio", session_id=str(self.sid), seconds=round(elapsed, 3))
                    await self.manager.events.emit(
                        self.sid, {"type": "turn_latency", "seconds": round(elapsed, 3)}
                    )
                first = False
            yield frame
        from pipecat.frames.frames import TTSStoppedFrame

        yield TTSStoppedFrame(context_id=context_id)


class VoiceGate(FrameProcessor):
    def __init__(self, manager, sid, operator=False):
        super().__init__()
        self.manager, self.sid, self.operator = manager, sid, operator
        self.human = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        runtime = self.manager.runtime.get(self.sid)
        human = runtime and runtime.get("human", False)
        if (
            isinstance(frame, VADUserStoppedSpeakingFrame)
            and direction == FrameDirection.DOWNSTREAM
            and runtime
            and not human
            and not self.operator
        ):
            # Estimate actual end of speech by removing the configured VAD silence.
            runtime["speech_end"] = time.perf_counter() - frame.stop_secs
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if human:
                other = runtime.get("client_output" if self.operator else "operator_output")
                if other:
                    await other.queue_frame(
                        OutputAudioRawFrame(frame.audio, frame.sample_rate, frame.num_channels)
                    )
                return
            if self.operator:
                return
        if (
            isinstance(frame, VADUserStartedSpeakingFrame)
            and direction == FrameDirection.DOWNSTREAM
            and not self.operator
            and not human
        ):
            start = time.monotonic()
            await self.broadcast_interruption()
            self.manager.metrics["interruptions"] += 1
            self.manager.metrics["server_interrupt_ms"] = round((time.monotonic() - start) * 1000, 1)
            await self.manager.events.emit(self.sid, {"type": "speech_started"})
        if isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            await self.manager.events.emit(
                self.sid, {"type": "bot_speaking", "speaking": isinstance(frame, BotStartedSpeakingFrame)}
            )
        if isinstance(frame, ErrorFrame):
            await self.manager.events.emit(
                self.sid,
                {
                    "type": "error",
                    "message": "Ошибка голосового конвейера. Можно продолжить в чате или оставить заявку.",
                },
            )
        await self.push_frame(frame, direction)


class DialogueProcessor(FrameProcessor):
    def __init__(self, manager, sid):
        super().__init__()
        self.manager, self.sid = manager, sid

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            runtime = self.manager.runtime.get(self.sid)
            if not runtime or runtime.get("human"):
                return
            try:
                if await self.manager.handle_consent(self.sid, frame.text):
                    return
                reply = await self.manager.dialogue.message(self.sid, frame.text, uuid4())
                await self.push_frame(TTSSpeakFrame(reply["text"]))
            except AppError as error:
                await self.push_frame(TTSSpeakFrame(error.message))
        else:
            await self.push_frame(frame, direction)


class VoiceManager:
    def __init__(self, business, models, dialogue, events):
        self.business, self.models, self.dialogue, self.events = business, models, dialogue, events
        self.active = None
        self.runtime = {}
        self.lock = asyncio.Lock()
        self.handoff_tasks = {}
        self.metrics = {"interruptions": 0, "server_interrupt_ms": None, "calls_started": 0, "calls_ended": 0}

    def build_pipeline(self, sid, transport, connection, profile, needs_consent=False):
        gate = VoiceGate(self, sid)
        vad = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(confidence=0.65, start_secs=0.15, stop_secs=0.95, min_volume=0.3),
            )
        )
        output = transport.output()
        pipeline = Pipeline(
            [
                transport.input(),
                vad,
                gate,
                LocalSTT(self.models, self.events, sid),
                DialogueProcessor(self, sid),
                LocalTTS(self.models, profile, self, sid),
                output,
            ]
        )
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
            idle_timeout_secs=300,
            cancel_runner_on_idle_timeout=False,
        )
        runtime = {
            "connection": connection,
            "worker": worker,
            "gate": gate,
            "client_output": output,
            "human": False,
            "needs_consent": needs_consent,
        }
        self.runtime[sid] = runtime
        return runtime

    async def start_external(self, sid, transport, connection, profile):
        async with self.lock:
            if self.active is not None:
                raise AppError("voice_busy", "Помощник занят", 409)
            self.active = sid
            runtime = self.build_pipeline(sid, transport, connection, profile, needs_consent=True)
            worker = runtime["worker"]

            @worker.event_handler("on_pipeline_finished")
            async def finished(worker, frame):
                if sid in self.runtime:
                    asyncio.create_task(self.end(sid, disconnected=True))

            runtime["run_task"] = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
            await worker.queue_frame(
                TTSSpeakFrame(
                    f"Здравствуйте. Меня зовут {'Мария' if profile == 'female' else 'Геннадий'}. Я виртуальный помощник. "
                    "Текст разговора и данные заявки сохраняются на тридцать дней. Аудио не сохраняется. "
                    "Если вы согласны, скажите да или нажмите один. Если не согласны, скажите нет."
                )
            )

            async def consent_timeout():
                await asyncio.sleep(45)
                if runtime.get("needs_consent"):
                    await worker.queue_frames(
                        [TTSSpeakFrame("Не удалось получить согласие. Завершаю разговор."), EndFrame()]
                    )

            runtime["consent_task"] = asyncio.create_task(consent_timeout())
            self.metrics["calls_started"] += 1

    async def handle_consent(self, sid, text):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("needs_consent"):
            return False
        if runtime.get("consent_rejected"):
            return True
        normalized = re.sub(r"[^а-яё\s]", "", text.lower()).strip()
        if normalized in (
            "да",
            "согласен",
            "согласна",
            "да согласен",
            "да согласна",
            "я согласен",
            "я согласна",
            "да я согласен",
            "да я согласна",
        ):
            await self.grant_consent(sid)
        elif normalized in ("нет", "не согласен", "не согласна", "я не согласен", "я не согласна"):
            runtime["consent_rejected"] = True
            await runtime["worker"].queue_frames([TTSSpeakFrame("Хорошо. Разговор завершается."), EndFrame()])
        else:
            await runtime["worker"].queue_frame(
                TTSSpeakFrame(
                    "Для продолжения нужно ваше согласие на сохранение текста разговора. Скажите да или нет."
                )
            )
        return True

    async def grant_consent(self, sid):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("needs_consent"):
            return
        if runtime.get("consent_rejected"):
            return
        await self.business.pool.execute(
            "UPDATE sessions SET consent=true WHERE id=$1 AND ended_at IS NULL", sid
        )
        from app.db import audit

        await audit(self.business.pool, "caller", "consent_granted", sid)
        runtime["needs_consent"] = False
        if runtime.get("gate"):
            await runtime["gate"].broadcast_interruption()
        if runtime.get("consent_task"):
            runtime["consent_task"].cancel()
        await self.events.emit(sid, {"type": "consent_granted"})
        await runtime["worker"].queue_frame(
            TTSSpeakFrame("Спасибо. Расскажите, пожалуйста, чем я могу помочь.")
        )

    async def dtmf(self, sid, digit):
        runtime = self.runtime.get(sid)
        if not runtime or runtime.get("human"):
            return
        if runtime.get("needs_consent"):
            if digit == "1":
                await self.grant_consent(sid)
            elif digit == "2":
                await self.handle_consent(sid, "нет")
            return
        if digit == "1" and await self.business.active_draft(sid):
            reply = await self.dialogue.message(sid, "да подтверждаю", uuid4())
            await runtime["worker"].queue_frame(TTSSpeakFrame(reply["text"]))
        elif digit == "0":
            operator = await self.business.pool.fetchval("SELECT value FROM settings WHERE key='operator'")
            if operator and operator.get("available"):
                await self.business.pool.execute(
                    "UPDATE sessions SET state='handoff_pending' WHERE id=$1 AND state='bot'", sid
                )
                self.request_handoff(sid)
                await self.events.emit(sid, {"type": "handoff_pending"})
                text = "Приглашаю сотрудника. Пожалуйста, подождите."
            else:
                text = "Сотрудник сейчас недоступен. Могу сохранить заявку на обратный звонок."
            await runtime["worker"].queue_frame(TTSSpeakFrame(text))

    async def offer(self, sid, sdp, type):
        async with self.lock:
            if self.active is not None:
                raise AppError("voice_busy", "Помощник занят. Попробуйте позже", 409)
            self.active = sid
            connection = SmallWebRTCConnection(ice_servers=[], connection_timeout_secs=20)
            try:
                await asyncio.wait_for(connection.initialize(sdp, type), 12)
                transport = SmallWebRTCTransport(
                    connection,
                    TransportParams(
                        audio_in_enabled=True,
                        audio_out_enabled=True,
                        audio_in_sample_rate=16000,
                        audio_out_sample_rate=24000,
                        audio_out_10ms_chunks=2,
                    ),
                )
                profile = await self.business.pool.fetchval(
                    "SELECT voice_profile FROM sessions WHERE id=$1", sid
                )
                runtime = self.build_pipeline(sid, transport, connection, profile or "male")
                worker = runtime["worker"]

                @transport.event_handler("on_client_connected")
                async def connected(transport, connection):
                    await worker.queue_frame(
                        TTSSpeakFrame(
                            f"Здравствуйте! Меня зовут {'Мария' if profile == 'female' else 'Геннадий'}. Я виртуальный помощник Точки. Чем могу помочь?"
                        )
                    )

                @transport.event_handler("on_client_disconnected")
                async def disconnected(transport, connection):
                    asyncio.create_task(self.end(sid, disconnected=True))

                @worker.event_handler("on_pipeline_finished")
                async def finished(worker, frame):
                    if sid in self.runtime:
                        asyncio.create_task(self.end(sid, disconnected=True))

                runner = PipelineRunner(handle_sigint=False)
                runtime["run_task"] = asyncio.create_task(runner.run(worker))
                self.metrics["calls_started"] += 1
                return {
                    key: value for key, value in connection.get_answer().items() if key in ("sdp", "type")
                }
            except Exception as exc:
                self.active = None
                await connection.disconnect()
                log.error("voice_offer_failed sid=%s error=%s", sid, type_name(exc))
                raise AppError(
                    "voice_connection_failed",
                    "Не удалось создать голосовое соединение. Попробуйте снова",
                    503,
                ) from None

    async def operator_offer(self, sid, sdp, type):
        runtime = self.runtime.get(sid)
        state = await self.business.pool.fetchval(
            "SELECT state FROM sessions WHERE id=$1 AND ended_at IS NULL", sid
        )
        if not runtime or state not in ("human", "handoff_pending"):
            raise AppError("voice_not_found", "Голосовой разговор недоступен", 409)
        if runtime.get("operator_worker"):
            raise AppError("operator_connected", "Сотрудник уже подключён", 409)
        connection = SmallWebRTCConnection(ice_servers=[], connection_timeout_secs=20)
        await connection.initialize(sdp, type)
        transport = SmallWebRTCTransport(
            connection,
            TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=16000,
                audio_out_sample_rate=24000,
                audio_out_10ms_chunks=2,
            ),
        )
        gate = VoiceGate(self, sid, operator=True)
        output = transport.output()
        worker = PipelineWorker(
            Pipeline([transport.input(), gate, output]),
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
            idle_timeout_secs=300,
            cancel_runner_on_idle_timeout=False,
        )
        runtime.update(operator_connection=connection, operator_worker=worker, operator_output=output)

        @transport.event_handler("on_client_connected")
        async def connected(transport, connection):
            await self.business.pool.execute(
                "UPDATE sessions SET state='human' WHERE id=$1 AND ended_at IS NULL", sid
            )
            runtime["human"] = True
            await self.interrupt(sid)
            await self.events.emit(
                sid, {"type": "human", "message": "Сотрудник подключился к голосовому разговору."}
            )

        @transport.event_handler("on_client_disconnected")
        async def disconnected(transport, connection):
            runtime["human"] = False
            await self.business.pool.execute(
                "UPDATE sessions SET state='bot' WHERE id=$1 AND ended_at IS NULL", sid
            )
            runtime.pop("operator_output", None)
            old = runtime.pop("operator_worker", None)
            if old:
                await old.cancel()
            await self.events.emit(
                sid, {"type": "handoff_timeout", "message": "Сотрудник отключился. Помощник снова на связи."}
            )

        runtime["operator_run_task"] = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
        return {key: value for key, value in connection.get_answer().items() if key in ("sdp", "type")}

    async def interrupt(self, sid):
        if runtime := self.runtime.get(sid):
            await runtime["gate"].broadcast_interruption()

    def request_handoff(self, sid):
        if sid in self.handoff_tasks:
            return

        async def timeout():
            try:
                await asyncio.sleep(30)
                row = await self.business.pool.fetchrow(
                    "UPDATE sessions SET state='bot' WHERE id=$1 AND state='handoff_pending' AND ended_at IS NULL RETURNING id",
                    sid,
                )
                if row:
                    await self.events.emit(
                        sid,
                        {
                            "type": "handoff_timeout",
                            "message": "Сотрудник не ответил. Помощник снова на связи; можно оставить заявку на обратный звонок.",
                        },
                    )
            finally:
                self.handoff_tasks.pop(sid, None)

        self.handoff_tasks[sid] = asyncio.create_task(timeout())

    async def end(self, sid, disconnected=False):
        runtime = self.runtime.pop(sid, None)
        if self.active == sid:
            self.active = None
        timeout = self.handoff_tasks.pop(sid, None)
        if timeout:
            timeout.cancel()
        if not runtime:
            return
        if runtime.get("consent_task"):
            runtime["consent_task"].cancel()
        for name in ("operator_worker", "worker"):
            if runtime.get(name):
                with contextlib_suppress():
                    await runtime[name].cancel()
        for name in ("operator_connection", "connection"):
            if runtime.get(name):
                with contextlib_suppress():
                    await runtime[name].disconnect()
        for name in ("operator_run_task", "run_task"):
            task = runtime.get(name)
            if task and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.metrics["calls_ended"] += 1
        if disconnected:
            await self.business.end(sid)
            self.dialogue.release(sid)
            await self.events.emit(sid, {"type": "ended"})

    async def close(self):
        for sid in list(self.runtime):
            await self.end(sid)
        for task in self.handoff_tasks.values():
            task.cancel()
        await asyncio.gather(*self.handoff_tasks.values(), return_exceptions=True)
        self.handoff_tasks.clear()


def type_name(exc):
    return exc.__class__.__name__


def contextlib_suppress():
    from contextlib import suppress

    return suppress(Exception)
