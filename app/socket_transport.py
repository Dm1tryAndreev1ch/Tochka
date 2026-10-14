"""Authenticated registration plus bounded Asterisk AudioSocket PCM transport."""

import asyncio
import contextlib
import struct
import time
from uuid import UUID

import numpy as np
import soxr
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from app.call_modes import CallModes
from app.config import settings
from app.errors import AppError
from app.telemetry import event

RATES = {0x10: 8000, 0x12: 16000, 0x13: 24000, 0x16: 48000}
TYPES = {rate: kind for kind, rate in RATES.items()}


async def read_packet(reader, timeout=60):
    async with asyncio.timeout(timeout):
        header = await reader.readexactly(3)
        kind, size = struct.unpack("!BH", header)
        if size > 4096:
            raise ValueError("AudioSocket packet too large")
        data = await reader.readexactly(size)
    return kind, data


class SocketConnection:
    def __init__(self, reader, writer, sample_rate):
        self.reader, self.writer, self.sample_rate = reader, writer, sample_rate
        self.finished = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.last_audio_send = 0

    async def send(self, kind, data):
        async with self.write_lock:
            if self.writer.is_closing():
                return False
            self.writer.write(struct.pack("!BH", kind, len(data)) + data)
            async with asyncio.timeout(3):
                await self.writer.drain()
            if kind in RATES:
                self.last_audio_send = time.monotonic()
            return True

    async def disconnect(self):
        self.finished.set()
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


class SocketInput(BaseInputTransport):
    def __init__(self, connection, params, manager, sid):
        super().__init__(params)
        self.connection, self.manager, self.sid = connection, manager, sid
        self.reader_task = None
        self.audio_seen = False
        self.resampler = soxr.ResampleStream(connection.sample_rate, 16000, 1, dtype="int16")

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._audio_in_queue = asyncio.Queue(maxsize=25)
        self.reader_task = self.create_task(self.read_audio())

    async def read_audio(self):
        try:
            while True:
                kind, payload = await read_packet(self.connection.reader)
                if kind == 0:
                    break
                if kind == 3:
                    if len(payload) != 1:
                        raise ValueError("Invalid DTMF")
                    await self.manager.dtmf(self.sid, payload.decode("ascii"))
                elif kind in RATES:
                    if not self.audio_seen:
                        event("socket_first_input_audio", sample_rate=RATES[kind], bytes=len(payload))
                        self.audio_seen = True
                    if RATES[kind] != self.connection.sample_rate or len(payload) % 2:
                        raise ValueError("Unexpected PCM format")
                    pcm = np.frombuffer(payload, dtype="<i2")
                    audio = self.resampler.resample_chunk(pcm).tobytes()
                    if audio:
                        await self.push_audio_frame(InputAudioRawFrame(audio, 16000, 1))
                elif kind == 0xFF:
                    break
                else:
                    raise ValueError("Unexpected AudioSocket packet")
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, ValueError):
            pass
        finally:
            self.connection.finished.set()

    async def stop_reader(self):
        if self.reader_task:
            await self.cancel_task(self.reader_task)
            self.reader_task = None

    async def stop(self, frame):
        await self.stop_reader()
        await super().stop(frame)

    async def cancel(self, frame):
        await self.stop_reader()
        await super().cancel(frame)


class SocketOutput(BaseOutputTransport):
    def __init__(self, connection, params):
        super().__init__(params)
        self.connection = connection
        self.next_audio = 0
        self.heartbeat_task = None

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        self.heartbeat_task = self.create_task(self.heartbeat())

    async def heartbeat(self):
        silence = b"\0" * (self.connection.sample_rate // 50 * 2)
        while not self.connection.writer.is_closing():
            await asyncio.sleep(0.02)
            if time.monotonic() - self.connection.last_audio_send >= 0.04:
                if not await self.connection.send(TYPES[self.connection.sample_rate], silence):
                    break

    async def stop_heartbeat(self):
        if self.heartbeat_task:
            await self.cancel_task(self.heartbeat_task)
            self.heartbeat_task = None

    async def stop(self, frame):
        await self.stop_heartbeat()
        await super().stop(frame)

    async def cancel(self, frame):
        await self.stop_heartbeat()
        await super().cancel(frame)

    async def write_audio_frame(self, frame):
        now = time.monotonic()
        self.next_audio = max(self.next_audio, now, self.connection.last_audio_send + 0.02)
        await asyncio.sleep(max(0, self.next_audio - now))
        sent = await self.connection.send(TYPES[self.connection.sample_rate], frame.audio)
        self.next_audio += len(frame.audio) / (self.connection.sample_rate * 2)
        return sent


class SocketTransport:
    def __init__(self, connection, manager, sid):
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=connection.sample_rate,
            audio_out_10ms_chunks=2,
        )
        self._input = SocketInput(connection, params, manager, sid)
        self._output = SocketOutput(connection, params)

    def input(self):
        return self._input

    def output(self):
        return self._output


class SocketCalls:
    def __init__(self, business, voice, models):
        self.business, self.voice, self.models = business, voice, models
        self.pending = {}
        self.connections = set()
        self.handlers = set()
        self.server = None
        self.registration_lock = asyncio.Lock()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", settings.socket_transport_port)
        event("socket_calls_listening", port=settings.socket_transport_port)

    async def register(self, source, profile):
        await CallModes(self.business.pool).require(source)
        if not self.models.ready or not await self.models.llm_ready():
            raise AppError("voice_unavailable", "Голосовые модели загружаются", 503)
        if profile == "female" and not (await self.models.tts_status()).get("natural_ready"):
            raise AppError("voice_unavailable", "Голос Марии пока недоступен", 503)
        async with self.registration_lock:
            await self.purge()
            if len(self.pending) + len(self.voice.runtime) >= settings.call_queue_capacity + 1:
                raise AppError("call_queue_full", "Очередь заполнена. Попробуйте позже", 429)
            session = await self.business.new_transport_session(source, profile)
            sid = UUID(session["id"])
            self.pending[sid] = {"source": source, "profile": profile, "expires": time.monotonic() + 30}
            return {
                "call_id": str(sid),
                "host": "127.0.0.1",
                "port": settings.socket_transport_port,
                "sample_rate": 8000 if source == "sip" else 48000,
            }

    async def purge(self):
        selected = (await CallModes(self.business.pool).selection())["mode"]
        for sid, lease in list(self.pending.items()):
            if lease["expires"] < time.monotonic() or lease["source"] != selected:
                self.pending.pop(sid, None)
                await self.business.pool.execute("DELETE FROM sessions WHERE id=$1 AND consent=false", sid)

    async def handle(self, reader, writer):
        if len(self.connections) >= 8:
            writer.close()
            return
        self.connections.add(writer)
        task = asyncio.current_task()
        self.handlers.add(task)
        sid = None
        connection = None
        try:
            kind, payload = await read_packet(reader, timeout=5)
            if kind != 1 or len(payload) != 16:
                raise ValueError("Missing call registration")
            proposed = UUID(bytes=payload)
            async with self.registration_lock:
                lease = self.pending.pop(proposed, None)
            if not lease or lease["expires"] < time.monotonic():
                raise ValueError("Call registration expired")
            sid = proposed
            await CallModes(self.business.pool).require(lease["source"])
            rate = 8000 if lease["source"] == "sip" else 48000
            connection = SocketConnection(reader, writer, rate)
            transport = SocketTransport(connection, self.voice, sid)
            await self.voice.start_external(sid, transport, connection, lease["profile"])
            await connection.finished.wait()
        except (AppError, ValueError, ConnectionError, TimeoutError, asyncio.IncompleteReadError) as exc:
            event("socket_call_closed", error_type=type(exc).__name__)
        finally:
            if sid:
                await self.voice.end(sid, disconnected=True)
                await self.business.pool.execute("DELETE FROM sessions WHERE id=$1 AND consent=false", sid)
            if connection:
                await connection.disconnect()
            else:
                writer.close()
            self.connections.discard(writer)
            self.handlers.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.connections):
            writer.close()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)
