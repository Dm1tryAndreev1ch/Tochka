"""Local WebRTC audio pipeline and a two-peer operator bridge."""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    SystemFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
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

from app.call_queue import CallQueue
from app.config import settings
from app.dialogue import uuid_from
from app.errors import AppError
from app.policy import notice
from app.speech import segments
from app.telemetry import event
from app.voice_phrases import ACKNOWLEDGEMENT, ACKNOWLEDGEMENT_MAX_BYTES

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


PUBLIC_CLARIFICATIONS = frozenset(
    {
        "На какую дату хотите записаться?",
        "В какое время вам удобно?",
        "Как к вам обращаться?",
        "Какой вопрос передать сотруднику?",
        "Назовите телефон с кодом страны, например плюс триста семьдесят пять, двадцать девять, сто двадцать три, сорок пять, шестьдесят семь. Можно ввести номер текстом.",
        "Назовите телефон с кодом страны или введите его текстом.",
    }
)
PUBLIC_SPEECH = PUBLIC_CLARIFICATIONS | {
    "Не удалось разобрать речь. Пожалуйста, повторите вопрос.",
    "Реплика слишком длинная. Пожалуйста, разделите её на несколько вопросов.",
    "Спасибо. Расскажите, пожалуйста, чем я могу помочь.",
    "Хорошо. Разговор завершается.",
    "Не удалось получить согласие. Завершаю разговор.",
    "Помощник сейчас занят. Вы ожидаете в очереди. Мы подключим вас, когда он освободится. Чтобы выйти, завершите звонок или нажмите два.",
    "Время ожидания истекло. Пожалуйста, позвоните позже или оставьте заявку на сайте.",
    "Сотрудник не ответил. Я снова на связи. Могу сохранить заявку на обратный звонок.",
}


async def voice_stage(events, sid, runtime, stage, *, at=None, generation=None, stt_utterance=None, **details):
    """One server clock and explicit turn identity; never include spoken text."""
    if events is None:
        return
    runtime = runtime or {}
    await events.emit(sid, {
        "type": "voice_stage", "stage": stage,
        "server_perf_seconds": round(time.perf_counter() if at is None else at, 6),
        "generation": runtime.get("generation") if generation is None else generation,
        "stt_utterance": runtime.get("stt_utterance") if stt_utterance is None else stt_utterance,
        "utterance_id": runtime.get("utterance_id"),
        **details,
    })


@dataclass
class STTAudioActivityFrame(SystemFrame):
    """Ordered observation of the same audio already forwarded by the VAD."""

    state: VADState = VADState.QUIET
    samples: int = 0


@dataclass
class PreparedTranscriptionFrame(TranscriptionFrame):
    """Final transcript carrying pending pure extraction, never a business action."""

    prepared: asyncio.Task | None = None
    stt_utterance: int | None = None


class TurnVADAnalyzer(SileroVADAnalyzer):
    """Retain longer pauses when the caller is dictating important fields."""

    def __init__(self, manager, sid, **kwargs):
        super().__init__(**kwargs)
        self.manager, self.sid = manager, sid
        self.state = VADState.QUIET

    async def analyze_audio(self, audio):
        runtime = self.manager.runtime.get(self.sid, {})
        if self.state == VADState.QUIET:
            sensitive = runtime.get("needs_consent") or runtime.get("sensitive_input")
            stop_secs = settings.vad_sensitive_stop_seconds if sensitive else settings.vad_stop_seconds
            if self.params.stop_secs != stop_secs:
                self.set_params(self.params.model_copy(update={"stop_secs": stop_secs}))
        self.state = await super().analyze_audio(audio)
        return self.state


class TurnVADProcessor(VADProcessor):
    def __init__(self, *, vad_analyzer, **kwargs):
        super().__init__(vad_analyzer=vad_analyzer, **kwargs)
        self.analyzer = vad_analyzer

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            await self.push_frame(
                STTAudioActivityFrame(state=self.analyzer.state, samples=len(frame.audio) // 2), direction
            )


@dataclass
class SpeechChunkSent(Frame):
    utterance_id: str = ""
    played_chars: int = 0
    expected_seconds: float = 0


@dataclass
class NoticeSent(Frame):
    policy_version: int = 0


@dataclass
class AcknowledgementAudioFrame(TTSAudioRawFrame):
    """Public waiting audio: never a reply, transcript or playback receipt."""


class DelayedAcknowledgement:
    """Play only resident PCM, with bounded output and no synthesis on the turn."""

    def __init__(self, manager, sid, runtime):
        self.manager, self.sid, self.runtime = manager, sid, runtime
        self.generation = runtime.get("generation", 0)
        self.task = None
        self.sent = False

    def valid(self):
        runtime = self.runtime
        return (
            self.manager.runtime.get(self.sid) is runtime
            and runtime.get("generation", 0) == self.generation
            and not any(runtime.get(key) for key in (
                "needs_consent", "consent_rejected", "human", "queued", "sensitive_input",
                "acknowledgement_sensitive", "recording_failure_task",
            ))
            and (not runtime.get("policy", {}).get("record_audio") or bool(runtime.get("capture")))
        )

    def start(self):
        if not settings.voice_acknowledgement_enabled or not self.valid():
            return
        getter = getattr(getattr(self.manager, "models", None), "cached_acknowledgement", None)
        pcm = getter(self.runtime.get("profile", "male")) if getter else None
        if not pcm or len(pcm) % 2 or len(pcm) > ACKNOWLEDGEMENT_MAX_BYTES or not self.runtime.get("client_output"):
            return
        self.runtime["acknowledgement"] = self
        self.task = asyncio.create_task(self.run(pcm))

    def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()

    async def run(self, pcm):
        try:
            end = self.runtime.get("speech_end", time.perf_counter())
            await asyncio.sleep(max(0, settings.voice_acknowledgement_delay_seconds - (time.perf_counter() - end)))
            if not self.valid():
                return
            output = self.runtime["client_output"]
            # Pace at 20 ms instead of filling the transport queue with the whole
            # clip: a ready answer or changed permission can stop immediately.
            for offset in range(0, len(pcm), 960):
                if not self.valid():
                    return
                if not self.sent:
                    await self.manager.events.emit(self.sid, {
                        "type": "acknowledgement", "text": ACKNOWLEDGEMENT,
                        "generation": self.generation, "phase": "queued",
                    })
                    if not self.valid():
                        return
                self.sent = True
                await output.queue_frame(AcknowledgementAudioFrame(pcm[offset:offset + 960], 24000, 1))
                await asyncio.sleep(0.02)
            await output.queue_frame(TTSStoppedFrame())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A best-effort acknowledgement must never replace the real answer.
            event("voice_acknowledgement_failed", session_id=str(self.sid), error_type=type(exc).__name__)

    async def stop(self):
        self.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        if self.sent and self.manager.runtime.get(self.sid) is self.runtime and self.runtime.get("generation", 0) == self.generation:
            # Target the transport only. Broadcasting through Dialogue/LocalTTS
            # would cancel useful generation or invalidate its playback history.
            try:
                await self.runtime["client_output"].queue_frame(InterruptionFrame())
            except Exception as exc:
                event("voice_acknowledgement_stop_failed", session_id=str(self.sid), error_type=type(exc).__name__)
            self.sent = False
        if self.runtime.get("acknowledgement") is self:
            self.runtime.pop("acknowledgement", None)


class DeliveryTracker(FrameProcessor):
    def __init__(self, manager, sid):
        super().__init__()
        self.manager, self.sid = manager, sid
        self.seconds = 0
        self.completed_seconds = 0
        self.audio_kind = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self.seconds = self.completed_seconds = 0
            self.audio_kind = None
        if isinstance(frame, OutputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self.seconds += len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
            runtime = self.manager.runtime.get(self.sid)
            kind = "acknowledgement" if isinstance(frame, AcknowledgementAudioFrame) else "response"
            if runtime and kind != self.audio_kind:
                self.audio_kind = kind
                elapsed = time.perf_counter() - runtime["speech_end"] if runtime.get("speech_end") else None
                await self.manager.events.emit(self.sid, {
                    "type": "voice_audio", "kind": kind,
                    "seconds": round(elapsed, 3) if elapsed is not None else None,
                    "generation": runtime.get("generation", 0),
                })
                if kind == "response" and runtime.get("utterance_id") and runtime.get("transport_audio_utterance") != runtime["utterance_id"]:
                    runtime["transport_audio_utterance"] = runtime["utterance_id"]
                    await voice_stage(self.manager.events, self.sid, runtime, "first_response_transport_pcm")
            if runtime and runtime.get("capture"):
                self.manager.capture_audio(self.sid, "assistant", frame.audio, frame.sample_rate, frame.num_channels)
        if isinstance(frame, NoticeSent) and direction == FrameDirection.DOWNSTREAM:
            runtime = self.manager.runtime.get(self.sid)
            if runtime:
                runtime["notice_delivered"] = True
                runtime["notice_completed_at"] = time.monotonic()
                self.manager.start_consent_timeout(self.sid)
        if isinstance(frame, TTSStoppedFrame) and direction == FrameDirection.DOWNSTREAM:
            self.completed_seconds, self.seconds = self.seconds, 0
            self.audio_kind = None
        if isinstance(frame, SpeechChunkSent) and direction == FrameDirection.DOWNSTREAM:
            runtime = self.manager.runtime.get(self.sid)
            if (
                runtime
                and runtime.get("utterance_id") == frame.utterance_id
                and self.completed_seconds + 0.025 >= frame.expected_seconds
            ):
                await self.manager.dialogue.audio_sent(
                    self.sid, uuid_from(frame.utterance_id), frame.played_chars
                )
                await self.manager.events.emit(
                    self.sid,
                    {
                        "type": "playback_marker",
                        "utterance_id": frame.utterance_id,
                        "played_chars": frame.played_chars,
                    },
                )
            self.completed_seconds = 0
        await self.push_frame(frame, direction)


class LocalSTT(SegmentedSTTService):
    def __init__(self, models, events, sid, manager=None):
        super().__init__(
            sample_rate=16000,
            audio_passthrough=False,
            trailing_silence_secs=0.3,
            settings=STTSettings(model="whisper-small", language=Language.RU),
            ttfs_p99_latency=8.0,
        )
        self.models, self.events, self.sid = models, events, sid
        self.manager = manager
        self._segment_queue = asyncio.Queue(maxsize=8)
        self._segments = deque()
        self._utterance = 0
        self._samples = 0
        self._partials = {}
        self._discarded = set()
        self._quiet_samples = 0
        self._preemptive_task = None
        self._preemptive_utterance = None
        self._preemptive_valid = False
        self._prepared_task = None
        self._prepared_utterance = None
        self._prepared_text = None
        self._prepared_valid = False
        self._prepare_started_utterance = None

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            self.cancel_preparation()
        if isinstance(frame, STTAudioActivityFrame):
            await FrameProcessor.process_frame(self, frame, direction)
            if direction == FrameDirection.DOWNSTREAM:
                self._observe_activity(frame)
            return
        await super().process_frame(frame, direction)

    def _observe_activity(self, frame):
        if frame.state in (VADState.STARTING, VADState.SPEAKING):
            self._quiet_samples = 0
            self._preemptive_valid = False
            self.cancel_preparation()
            return
        if not self._user_speaking or self._utterance in self._discarded:
            return
        self._quiet_samples += frame.samples
        if (
            not settings.stt_preemptive_enabled
            or self._quiet_samples < self.sample_rate * settings.stt_preemptive_silence_seconds
            or self._samples < self.sample_rate * 0.4
            or self._segments
            or self._preemptive_valid
            or (self._preemptive_task is not None and not self._preemptive_task.done())
        ):
            return
        # The same model lock is used by final STT. At most one speculative
        # inference is in flight, and no partial text reaches the dialogue.
        audio = bytes(self._audio_buffer) + self._trailing_silence()
        self._preemptive_task = asyncio.create_task(asyncio.to_thread(self.models.transcribe, audio))
        self._preemptive_utterance = self._utterance
        self._preemptive_valid = True
        self._preemptive_task.add_done_callback(self._early_transcribed)

    def _early_transcribed(self, task):
        self._consume_preemptive_error(task)
        runtime = self.manager.runtime.get(self.sid) if self.manager else None
        if (
            task.cancelled() or task.exception() is not None
            or task is not self._preemptive_task
            or not self._preemptive_valid or self._preemptive_utterance != self._utterance
            or not runtime or runtime.get("needs_consent") or runtime.get("human") or runtime.get("queued")
            or runtime.get("stt", self) is not self
            or self._prepare_started_utterance == self._utterance
            or (self._prepared_task is not None and not self._prepared_task.done())
        ):
            return
        text = " ".join(task.result().split())
        if not text or not hasattr(self.manager.dialogue, "prepare"):
            return
        self._prepare_started_utterance = self._utterance
        self._prepared_utterance = self._utterance
        self._prepared_text = text
        self._prepared_valid = True
        self._prepared_task = asyncio.create_task(self._prepare_timed(text, runtime, self._utterance, runtime.get("generation", 0)))
        self._prepared_task.add_done_callback(self._consume_preemptive_error)

    async def _prepare_timed(self, text, runtime, utterance, generation):
        identity = {"generation": generation, "stt_utterance": utterance, "utterance_id": None}
        await voice_stage(self.events, self.sid, runtime, "prepare_started", **identity)
        try:
            result = await self.manager.dialogue.prepare(self.sid, text)
        except asyncio.CancelledError:
            await voice_stage(self.events, self.sid, runtime, "prepare_cancelled", **identity)
            raise
        except Exception:
            await voice_stage(self.events, self.sid, runtime, "prepare_failed", **identity)
            raise
        await voice_stage(self.events, self.sid, runtime, "prepare_ready", result_available=result is not None, **identity)
        return result

    def cancel_preparation(self):
        self._preemptive_valid = False
        self._prepared_valid = False
        if self._prepared_task is not None and not self._prepared_task.done():
            self._prepared_task.cancel()

    async def _cancel_speculation(self):
        self.cancel_preparation()
        tasks = [task for task in (self._preemptive_task, self._prepared_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._preemptive_task = self._prepared_task = None

    @staticmethod
    def _consume_preemptive_error(task):
        if not task.cancelled():
            task.exception()

    async def cancel(self, frame):
        await self._cancel_speculation()
        await super().cancel(frame)

    async def cleanup(self):
        await self._cancel_speculation()
        await super().cleanup()

    @property
    def wants_wav_segments(self):
        return False

    async def process_audio_frame(self, frame, direction):
        await super().process_audio_frame(frame, direction)
        if not self._user_speaking:
            return
        self._samples += len(frame.audio) // 2
        if self._samples > 16000 * 90:
            self.cancel_preparation()
            self._discarded.add(self._utterance)
            self._partials.pop(self._utterance, None)
            self._audio_buffer.clear()
            return
        size, overlap = 16000 * 2 * 20, 16000
        while len(self._audio_buffer) > size:
            self._preemptive_valid = False
            self.cancel_preparation()
            audio = bytes(self._audio_buffer[:size])
            del self._audio_buffer[: size - overlap]
            await self.queue_segment(audio + self._trailing_silence(), final=False)

    async def _handle_user_started_speaking(self, frame):
        self.cancel_preparation()
        await super()._handle_user_started_speaking(frame)
        self._utterance += 1
        self._samples = 0
        self._quiet_samples = 0
        self._preemptive_valid = False
        # Keep defensive discard bookkeeping bounded over long calls.
        self._discarded = {u for u in self._discarded if u >= self._utterance - 8}

    async def queue_segment(self, audio, *, final, transcription=None):
        self._segments.append((self._utterance, final, transcription))
        await self._segment_queue.put(audio)

    async def _handle_user_stopped_speaking(self, frame):
        self._user_speaking = False
        if self._utterance in self._discarded:
            self._audio_buffer.clear()
            await self.events.emit(
                self.sid,
                {
                    "type": "error",
                    "message": "Реплика длиннее полутора минут. Разделите её на несколько вопросов.",
                },
            )
            await self.push_frame(
                TTSSpeakFrame("Реплика слишком длинная. Пожалуйста, разделите её на несколько вопросов.")
            )
            return
        audio = bytes(self._audio_buffer) + self._trailing_silence()
        self._audio_buffer.clear()
        transcription = None
        if self._preemptive_valid and self._preemptive_utterance == self._utterance:
            transcription = self._preemptive_task
            # Retain the reference until completion so another utterance cannot
            # queue a second early inference while this one is still running.
        self._preemptive_valid = False
        await self.queue_segment(audio, final=True, transcription=transcription)

    async def run_stt(self, audio):
        utterance, final, transcription = self._segments.popleft() if self._segments else (self._utterance, True, None)
        if utterance in self._discarded or (final and utterance != self._utterance):
            self._partials.pop(utterance, None)
            return
        try:
            text = await transcription if transcription is not None else await asyncio.to_thread(self.models.transcribe, audio)
        except Exception:
            if transcription is None:
                raise
            # A failed early inference must not prevent the complete final one.
            text = await asyncio.to_thread(self.models.transcribe, audio)
        if utterance in self._discarded:
            return
        if final and utterance != self._utterance:
            self._partials.pop(utterance, None)
            return
        previous = self._partials.get(utterance, "")
        words, incoming = previous.split(), text.split()
        overlap = 0
        for count in range(2, min(12, len(words), len(incoming)) + 1):
            if [w.casefold().strip(".,!?") for w in words[-count:]] == [
                w.casefold().strip(".,!?") for w in incoming[:count]
            ]:
                overlap = count
        combined = " ".join(words + incoming[overlap:])
        if final:
            self._partials.pop(utterance, None)
            if combined:
                prepared = (
                    self._prepared_task
                    if self._prepared_valid and self._prepared_utterance == utterance and self._prepared_text == combined
                    else None
                )
                runtime = self.manager.runtime.get(self.sid) if self.manager else None
                if runtime is not None:
                    runtime["stt_utterance"] = utterance
                await voice_stage(
                    self.events, self.sid, runtime, "stt_final_ready", stt_utterance=utterance,
                    prepared_available=prepared is not None,
                    prepared_done=prepared.done() if prepared is not None else False,
                    preemptive_attempted=transcription is not None,
                )
                yield PreparedTranscriptionFrame(
                    combined, str(self.sid), datetime.now(UTC).isoformat(), prepared=prepared, stt_utterance=utterance,
                )
            else:
                yield TTSSpeakFrame("Не удалось разобрать речь. Пожалуйста, повторите вопрос.")
        else:
            self._partials[utterance] = combined


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
        runtime = self.manager.runtime.get(self.sid) if self.manager else None
        utterance = runtime.get("utterance_id") if runtime else None
        is_notice = runtime and text == runtime.get("notice_text")
        if runtime and runtime.get("speech_end") is not None and not is_notice:
            await voice_stage(self.manager.events, self.sid, runtime, "tts_requested")
        cacheable = text in PUBLIC_SPEECH or bool(
            runtime and (text == runtime.get("cacheable_text") or is_notice)
        )
        prefix = 0
        for part in segments(text):
            audio_seconds = 0

            async def chunks():
                nonlocal audio_seconds
                async for audio in self.models.stream_speech(part, self.voice_profile, cache=cacheable):
                    audio_seconds += len(audio) / 48000
                    yield audio

            async for frame in self._stream_audio_frames_from_iterator(
                chunks(), in_sample_rate=24000, context_id=context_id
            ):
                if first and self.manager:
                    end = runtime.pop("speech_end", None) if runtime else None
                    if end is not None:
                        now = time.perf_counter()
                        elapsed = now - end
                        await voice_stage(self.manager.events, self.sid, runtime, "first_useful_server_pcm", at=now)
                        self.models._metric("turn_first_audio_seconds", elapsed)
                        event("turn_first_audio", session_id=str(self.sid), seconds=round(elapsed, 3))
                        await self.manager.events.emit(
                            self.sid, {"type": "turn_latency", "kind": "response", "seconds": round(elapsed, 3)}
                        )
                    first = False
                yield frame
            # Flush the final partial audio chunk before the marker follows the
            # transport's paced audio queue. The marker is delivery, not playout.
            yield TTSStoppedFrame(context_id=context_id)
            start = text.find(part, prefix)
            prefix = start + len(part) if start >= 0 else min(len(text), prefix + len(part))
            if utterance and audio_seconds:
                yield SpeechChunkSent(
                    utterance_id=utterance, played_chars=prefix, expected_seconds=audio_seconds
                )
        if is_notice:
            yield NoticeSent(policy_version=runtime["policy"].get("policy_version", 1))


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
            await voice_stage(self.manager.events, self.sid, runtime, "speech_endpoint", speech_end_perf_seconds=round(runtime["speech_end"], 6))
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if human:
                if self.operator:
                    self.manager.capture_audio(self.sid, "operator", frame.audio, frame.sample_rate, frame.num_channels)
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
            if runtime and runtime.get("needs_consent"):
                runtime["speech_after_notice"] = (
                    runtime.get("notice_delivered", False)
                    and start - runtime.get("notice_completed_at", start) >= 0.5
                )
            if not runtime or not runtime.get("needs_consent") or runtime.get("notice_delivered"):
                await self.manager.interrupt(self.sid)
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


class AdmissionGate(FrameProcessor):
    def __init__(self, manager, sid):
        super().__init__()
        self.manager, self.sid = manager, sid

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        runtime = self.manager.runtime.get(self.sid)
        if (
            direction == FrameDirection.DOWNSTREAM
            and runtime
            and runtime.get("queued")
            and isinstance(frame, InputAudioRawFrame)
        ):
            return
        if (
            direction == FrameDirection.DOWNSTREAM
            and runtime
            and runtime.get("capture")
            and isinstance(frame, InputAudioRawFrame)
        ):
            self.manager.capture_audio(self.sid, "caller", frame.audio, frame.sample_rate, frame.num_channels)
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
                generation = runtime.get("generation", 0)
                prepared = frame.prepared if isinstance(frame, PreparedTranscriptionFrame) else None
                await voice_stage(getattr(self.manager, "events", None), self.sid, runtime, "dialogue_started", prepared_available=prepared is not None)
                acknowledgement = DelayedAcknowledgement(self.manager, self.sid, runtime)
                # Dictated personal details do not need a waiting prompt.
                if not re.search(r"\d|@|\bменя зовут\b|\bмой (?:номер|телефон)\b", frame.text, re.IGNORECASE):
                    acknowledgement.start()
                try:
                    if prepared is None:
                        reply = await self.manager.dialogue.message(self.sid, frame.text, uuid4())
                    else:
                        reply = await self.manager.dialogue.message(self.sid, frame.text, uuid4(), prepared=prepared)
                    dialogue_done = time.perf_counter()
                finally:
                    await acknowledgement.stop()
                if self.manager.runtime.get(self.sid) is not runtime or runtime.get("generation", 0) != generation:
                    await self.manager.dialogue.interrupt_playback(self.sid)
                    return
                runtime["utterance_id"] = reply.get("utterance_id")
                await voice_stage(getattr(self.manager, "events", None), self.sid, runtime, "dialogue_done", at=dialogue_done, response_source=reply.get("response_source"))
                runtime["sensitive_input"] = bool(reply.get("draft")) or bool(
                    re.search(r"(?:назовите|укажите|на какую|в какое|подтверд).*(?:телефон|номер|дат|время|запис|заявк)", reply["text"], re.IGNORECASE)
                )
                runtime["cacheable_text"] = (
                    reply["text"]
                    if reply.get("response_source") in {"knowledge", "missing_fact"}
                    or ("collected" in reply and reply["text"] in PUBLIC_CLARIFICATIONS)
                    else None
                )
                runtime["acknowledgement_sensitive"] = bool(reply.get("draft") or reply.get("collected"))
                await self.push_frame(TTSSpeakFrame(reply["text"]))
            except AppError as error:
                await self.push_frame(TTSSpeakFrame(error.message))
        else:
            await self.push_frame(frame, direction)


class VoiceManager:
    def __init__(self, business, models, dialogue, events):
        self.business, self.models, self.dialogue, self.events = business, models, dialogue, events
        self.queue = CallQueue(settings.call_queue_capacity)
        self.runtime = {}
        self.lock = asyncio.Lock()
        self.handoff_tasks = {}
        self.metrics = {"interruptions": 0, "server_interrupt_ms": None, "calls_started": 0, "calls_ended": 0}
        self.closing = False

    @property
    def active(self):
        return self.queue.active

    @active.setter
    def active(self, sid):
        self.queue.active = sid

    def capture_audio(self, sid, speaker, audio, sample_rate, channels):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("capture"):
            return
        try:
            runtime["capture"].append(speaker, audio, sample_rate, channels)
        except OSError as exc:
            capture = runtime.pop("capture")
            with contextlib_suppress():
                capture.file.close()
            event("audio_recording_failed", session_id=str(sid), error_type=type(exc).__name__)

            async def stop_after_failure():
                await self.events.emit(sid, {"type": "error", "message": "Аудиозапись прервана из-за ошибки хранилища. Разговор завершается."})
                if self.runtime.get(sid) is runtime:
                    await runtime["worker"].queue_frames([
                        TTSSpeakFrame("Ошибка аудиозаписи. Пожалуйста, позвоните позже. Завершаю разговор."),
                        EndFrame(),
                    ])

            runtime["recording_failure_task"] = asyncio.create_task(stop_after_failure())

    async def queue_positions(self):
        for sid in self.queue.waiting:
            await self.events.emit(
                sid,
                {
                    "type": "queue_update",
                    "position": self.queue.position(sid),
                    "waiting": len(self.queue.waiting),
                },
            )

    async def activate(self, sid):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("connected") or runtime.get("activated"):
            return
        runtime.update(queued=False, activated=True)
        if task := runtime.pop("queue_timeout", None):
            task.cancel()
        await self.business.pool.execute(
            "UPDATE sessions SET state=CASE WHEN state='queued' THEN 'bot' ELSE state END,started_at=coalesce(started_at,now()) WHERE id=$1",
            sid,
        )
        await self.events.emit(sid, {"type": "queue_ready", "position": 0})
        profile = runtime["profile"]
        greeting = f"Здравствуйте. Меня зовут {'Мария' if profile == 'female' else 'Геннадий'}. Я виртуальный помощник. "
        if runtime.get("needs_consent"):
            greeting += notice(runtime.get("policy", {}))
            runtime["notice_text"] = greeting
            runtime["notice_delivered"] = False

        else:
            greeting += "Чем могу помочь?"
        runtime["cacheable_text"] = greeting
        await runtime["worker"].queue_frame(TTSSpeakFrame(greeting))

    def start_consent_timeout(self, sid):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("needs_consent") or runtime.get("consent_task"):
            return

        async def consent_timeout():
            await asyncio.sleep(45)
            if self.runtime.get(sid) is runtime and runtime.get("needs_consent"):
                await runtime["worker"].queue_frames([
                    TTSSpeakFrame("Не удалось получить согласие. Завершаю разговор."), EndFrame(),
                ])

        runtime["consent_task"] = asyncio.create_task(consent_timeout())

    async def connected(self, sid):
        runtime = self.runtime[sid]
        runtime["connected"] = True
        await self.business.pool.execute(
            "UPDATE sessions SET connected_at=coalesce(connected_at,now()) WHERE id=$1", sid
        )
        if self.active == sid:
            await self.activate(sid)
        else:
            runtime["queued"] = True
            await self.business.pool.execute("UPDATE sessions SET state='queued' WHERE id=$1", sid)
            await self.queue_positions()
            await runtime["worker"].queue_frame(
                TTSSpeakFrame(
                    "Помощник сейчас занят. Вы ожидаете в очереди. Мы подключим вас, когда он освободится. Чтобы выйти, завершите звонок или нажмите два."
                )
            )

            async def expire():
                await asyncio.sleep(settings.call_queue_wait_seconds)
                if runtime.get("queued"):
                    await self.events.emit(
                        sid,
                        {
                            "type": "queue_timeout",
                            "message": "Время ожидания истекло. Можно оставить заявку или позвонить позже.",
                        },
                    )
                    await runtime["worker"].queue_frames(
                        [
                            TTSSpeakFrame(
                                "Время ожидания истекло. Пожалуйста, позвоните позже или оставьте заявку на сайте."
                            ),
                            EndFrame(),
                        ]
                    )

            runtime["queue_timeout"] = asyncio.create_task(expire())

    def build_pipeline(self, sid, transport, connection, profile, needs_consent=False):
        gate = VoiceGate(self, sid)
        vad = TurnVADProcessor(
            vad_analyzer=TurnVADAnalyzer(
                self, sid,
                sample_rate=16000,
                params=VADParams(
                    confidence=0.65, start_secs=0.15,
                    stop_secs=settings.vad_sensitive_stop_seconds if needs_consent else settings.vad_stop_seconds,
                    min_volume=0.3,
                ),
            )
        )
        output = transport.output()
        stt = LocalSTT(self.models, self.events, sid, self)
        pipeline = Pipeline(
            [
                transport.input(),
                AdmissionGate(self, sid),
                vad,
                gate,
                stt,
                DialogueProcessor(self, sid),
                LocalTTS(self.models, profile, self, sid),
                output,
                DeliveryTracker(self, sid),
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
            "stt": stt,
            "client_output": output,
            "human": False,
            "needs_consent": needs_consent,
            "sensitive_input": False,
            "generation": 0,
            "utterance_id": None,
            "profile": profile,
            "queued": self.active != sid,
            "connected": False,
            "activated": False,
            "policy": {},
        }
        self.runtime[sid] = runtime
        return runtime

    async def start_external(self, sid, transport, connection, profile):
        async with self.lock:
            self.queue.join(sid)
            runtime = self.build_pipeline(sid, transport, connection, profile, needs_consent=True)
            runtime["policy"] = (
                await self.business.pool.fetchval("SELECT policy FROM sessions WHERE id=$1", sid) or {}
            )
            worker = runtime["worker"]

            @worker.event_handler("on_pipeline_finished")
            async def finished(worker, frame):
                if sid in self.runtime:
                    asyncio.create_task(self.end(sid, disconnected=True))

            runtime["run_task"] = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
            await self.connected(sid)
            self.metrics["calls_started"] += 1

    async def handle_consent(self, sid, text):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("needs_consent"):
            return False
        if runtime.get("consent_rejected"):
            return True
        normalized = " ".join(re.sub(r"[^а-яё\s]", "", text.lower()).split())
        allowed = normalized in (
            "да",
            "согласен",
            "согласна",
            "да согласен",
            "да согласна",
            "я согласен",
            "я согласна",
            "да я согласен",
            "да я согласна",
            "разрешаю",
            "я разрешаю",
            "я разрешаю запись разговора",
            "да разрешаю запись разговора",
            "да разрешаю",
            "разрешаю запись",
            "разрешаю запись разговора",
            "разрешаю записать разговор",
            "можете записывать",
        )
        if allowed:
            if not runtime.get("speech_after_notice", False):
                phrase = "разрешаю запись разговора" if runtime.get("policy", {}).get("record_audio") else "согласен"
                await runtime["worker"].queue_frame(
                    TTSSpeakFrame(
                        f"Пожалуйста, после уведомления подтвердите: {phrase}. До разрешения запись не ведётся."
                    )
                )
                return True
            await self.grant_consent(sid, oral_text=normalized)
        elif normalized == "нет" or re.search(
            r"\b(?:не\s+(?:разрешаю|согласен|согласна)|не записывайте|без записи|только текст)\b", normalized
        ):
            runtime["consent_rejected"] = True
            await runtime["worker"].queue_frames([TTSSpeakFrame("Хорошо. Разговор завершается."), EndFrame()])
        else:
            prompt = "Скажите «разрешаю запись разговора» или «не разрешаю»." if runtime.get("policy", {}).get("record_audio") else "Скажите «согласен» или «не согласен»."
            await runtime["worker"].queue_frame(
                TTSSpeakFrame(
                    "Для продолжения нужно ваше согласие. " + prompt
                )
            )
        return True

    async def grant_consent(self, sid, oral_text=None):
        runtime = self.runtime.get(sid)
        if not runtime or not runtime.get("needs_consent"):
            return
        if runtime.get("consent_rejected"):
            return
        audio = bool(runtime.get("policy", {}).get("record_audio") and oral_text)
        capture = None
        if audio:
            try:
                if not getattr(self, "archive", None):
                    raise OSError("Recording store unavailable")
                capture = self.archive.start_capture(sid)
            except OSError as exc:
                runtime["consent_rejected"] = True
                event("audio_recording_failed", session_id=str(sid), error_type=type(exc).__name__)
                await self.events.emit(sid, {"type": "error", "message": "Не удалось начать аудиозапись. Разговор завершается."})
                await runtime["worker"].queue_frames([
                    TTSSpeakFrame("Не удалось начать аудиозапись. Пожалуйста, позвоните позже. Завершаю разговор."),
                    EndFrame(),
                ])
                return
        from app.db import audit

        try:
            async with self.business.pool.acquire() as conn, conn.transaction():
                updated = await conn.fetchval(
                    "UPDATE sessions SET consent=true,audio_consent=$2,consent_at=now(),consent_method=$3 "
                    "WHERE id=$1 AND ended_at IS NULL AND NOT data_erased AND expires_at>now() RETURNING id",
                    sid, audio, "oral" if oral_text else "dtmf",
                )
                if not updated:
                    raise AppError("session_expired", "Разговор завершён", 401)
                await audit(conn, "caller", "consent_granted", sid, {
                    "audio": audio, "method": "oral" if oral_text else "dtmf",
                    "policy_version": runtime.get("policy", {}).get("policy_version"),
                })
        except BaseException:
            if capture:
                with contextlib_suppress():
                    capture.file.close()
                self.archive.store.delete(sid)
            raise
        if capture:
            runtime["capture"] = capture
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
        if runtime.get("queued"):
            if digit == "2":
                await self.end(sid)
            return
        if runtime.get("needs_consent"):
            if digit == "1":
                if runtime.get("policy", {}).get("record_audio"):
                    await runtime["worker"].queue_frame(
                        TTSSpeakFrame(
                            "Для разрешения аудиозаписи нужно устное подтверждение. Скажите: разрешаю запись разговора."
                        )
                    )
                elif runtime.get("notice_delivered"):
                    await self.grant_consent(sid)
            elif digit == "2":
                await self.handle_consent(sid, "нет")
            return
        if digit == "1" and await self.business.active_draft(sid):
            reply = await self.dialogue.message(sid, "да подтверждаю", uuid4())
            await runtime["worker"].queue_frame(TTSSpeakFrame(reply["text"]))
        elif digit == "0":
            try:
                result = await self.dialogue.request_handoff(sid)
                if result["status"] == "pending":
                    self.request_handoff(sid)
                text = result["message"]
            except AppError as exc:
                text = exc.message
            await runtime["worker"].queue_frame(TTSSpeakFrame(text))

    async def offer(self, sid, sdp, type):
        async with self.lock:
            if sid in self.runtime:
                raise AppError("voice_connected", "Этот звонок уже подключён", 409)
            self.queue.join(sid)
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
                runtime["policy"] = (
                    await self.business.pool.fetchval("SELECT policy FROM sessions WHERE id=$1", sid) or {}
                )
                existing_consent = await self.business.pool.fetchval(
                    "SELECT consent FROM sessions WHERE id=$1", sid
                )
                runtime["needs_consent"] = bool(runtime["policy"].get("record_audio")) or not existing_consent
                worker = runtime["worker"]

                @transport.event_handler("on_client_connected")
                async def connected(transport, connection):
                    await self.connected(sid)

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
                promoted = self.queue.release(sid)
                if promoted:
                    await self.activate(promoted)
                await connection.disconnect()
                log.error("voice_offer_failed sid=%s error=%s", sid, type_name(exc))
                raise AppError(
                    "voice_connection_failed",
                    "Не удалось создать голосовое соединение. Попробуйте снова",
                    503,
                ) from None

    async def operator_offer(self, sid, sdp, type, principal=None):
        runtime = self.runtime.get(sid)
        state = await self.business.pool.fetchval(
            "SELECT state FROM sessions WHERE id=$1 AND ended_at IS NULL", sid
        )
        if not runtime or state not in ("human", "handoff_pending"):
            raise AppError("voice_not_found", "Голосовой разговор недоступен", 409)
        if runtime.get("operator_worker"):
            raise AppError("operator_connected", "Сотрудник уже подключён", 409)
        if (
            principal
            and await self.business.pool.fetchval("SELECT assigned_user_id FROM sessions WHERE id=$1", sid)
            != principal.user_id
        ):
            raise AppError("forbidden", "Этот разговор принят другим сотрудником", 403)
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
        if principal:
            runtime["operator_owner"] = {"user_id": principal.user_id, "session_hash": principal.session_hash}

            async def validate_access():
                while sid in self.runtime and runtime.get("operator_connection") is connection:
                    await asyncio.sleep(5)
                    valid = await self.business.pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM admin_sessions s JOIN staff_users u ON u.id=s.user_id WHERE s.token_hash=$1 AND s.expires_at>now() AND u.active AND u.permissions @> '[\"calls.live\"]'::jsonb)",
                        principal.session_hash,
                    )
                    if not valid:
                        await connection.disconnect()
                        return

            runtime["operator_access_task"] = asyncio.create_task(validate_access())

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
            runtime.pop("operator_owner", None)
            task = runtime.pop("operator_access_task", None)
            if task and task is not asyncio.current_task():
                task.cancel()
            old = runtime.pop("operator_worker", None)
            if old:
                await old.cancel()
            await self.events.emit(
                sid, {"type": "handoff_timeout", "message": "Сотрудник отключился. Помощник снова на связи."}
            )
            await runtime["worker"].queue_frame(
                TTSSpeakFrame("Сотрудник отключился. Я снова на связи. Чем помочь?")
            )

        runtime["operator_run_task"] = asyncio.create_task(PipelineRunner(handle_sigint=False).run(worker))
        return {key: value for key, value in connection.get_answer().items() if key in ("sdp", "type")}

    async def revoke_staff(self, *, user_id=None, session_hash=None):
        for runtime in list(self.runtime.values()):
            owner = runtime.get("operator_owner", {})
            if (user_id and owner.get("user_id") == user_id) or (
                session_hash and owner.get("session_hash") == session_hash
            ):
                connection = runtime.get("operator_connection")
                if connection:
                    await connection.disconnect()

    async def interrupt(self, sid):
        if runtime := self.runtime.get(sid):
            if acknowledgement := runtime.get("acknowledgement"):
                acknowledgement.cancel()
            if stt := runtime.get("stt"):
                stt.cancel_preparation()
            runtime["generation"] = runtime.get("generation", 0) + 1
            runtime["utterance_id"] = None
            runtime["stt_utterance"] = None
            await self.dialogue.interrupt_playback(sid)
            await runtime["gate"].broadcast_interruption()

    def request_handoff(self, sid, timeout_seconds=30):
        if sid in self.handoff_tasks:
            return

        async def timeout():
            try:
                await asyncio.sleep(timeout_seconds)
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
                    if runtime := self.runtime.get(sid):
                        await runtime["worker"].queue_frame(
                            TTSSpeakFrame(
                                "Сотрудник не ответил. Я снова на связи. Могу сохранить заявку на обратный звонок."
                            )
                        )
            finally:
                self.handoff_tasks.pop(sid, None)

        self.handoff_tasks[sid] = asyncio.create_task(timeout())

    async def end(self, sid, disconnected=False):
        runtime = self.runtime.pop(sid, None)
        promoted = self.queue.release(sid)
        timeout = self.handoff_tasks.pop(sid, None)
        if timeout:
            timeout.cancel()
        if not runtime:
            if promoted and not self.closing:
                await self.activate(promoted)
            await self.queue_positions()
            return
        if acknowledgement := runtime.get("acknowledgement"):
            acknowledgement.cancel()
        if task := runtime.get("queue_timeout"):
            task.cancel()
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
        for name in ("operator_run_task", "operator_access_task", "run_task", "recording_failure_task"):
            task = runtime.get(name)
            if task and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.metrics["calls_ended"] += 1
        if getattr(self, "archive", None):
            try:
                await self.archive.finalize(sid, runtime.get("capture"))
            except Exception as exc:
                event("archive_finalize_failed", session_id=str(sid), error_type=type(exc).__name__)
        if promoted and not self.closing:
            await self.activate(promoted)
        await self.queue_positions()
        if disconnected:
            await self.business.end(sid)
            self.dialogue.release(sid)
            await self.events.emit(sid, {"type": "ended"})

    async def close(self):
        self.closing = True
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
