"""Synthetic authenticated SIP/RTP call to the local PBX, with no external phone number."""

import asyncio
import audioop
import hashlib
import json
import re
import secrets
import socket
import struct
import sys
import wave
from pathlib import Path

import httpx
import numpy as np
import soxr
from piper import PiperVoice

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import ROOT, settings  # noqa: E402


class SIPCaller:
    def __init__(self, host, port, account, extension):
        self.host, self.port, self.account, self.extension = host, port, account, extension
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind((host, 0))
        self.sip.setblocking(False)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind((host, 0))
        self.rtp.setblocking(False)
        self.call_id = secrets.token_hex(12)
        self.from_tag = secrets.token_hex(6)
        self.to_tag = ""
        self.seq = 0
        self.stamp = 0
        self.ssrc = secrets.randbits(32)
        self.remote_rtp = None
        self.audio = bytearray()
        self.cseq = 1
        self.uri = f"sip:{extension}@{host}:{port}"

    def message(self, method, body="", auth="", branch=None):
        branch = branch or "z9hG4bK" + secrets.token_hex(8)
        user = self.account["username"]
        local = self.sip.getsockname()[1]
        to = f"<{self.uri}>" + (" ;tag=" + self.to_tag if self.to_tag else "")
        headers = [
            f"{method} {self.uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.host}:{local};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{user}@{self.host}>;tag={self.from_tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} {method}",
            f"Contact: <sip:{user}@{self.host}:{local}>",
        ]
        if auth:
            headers.append(auth)
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append(f"Content-Length: {len(body.encode())}")
        return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()

    async def send(self, data):
        await asyncio.get_running_loop().sock_sendto(self.sip, data, (self.host, self.port))

    async def answer(self):
        async with asyncio.timeout(12):
            while True:
                data, _ = await asyncio.get_running_loop().sock_recvfrom(self.sip, 65536)
                text = data.decode(errors="replace")
                if text.startswith("SIP/2.0 ") and int(text.split()[1]) >= 200:
                    return text

    async def connect(self):
        sdp = f"v=0\r\no=voice 1 1 IN IP4 {self.host}\r\ns=Voice acceptance\r\nc=IN IP4 {self.host}\r\nt=0 0\r\nm=audio {self.rtp.getsockname()[1]} RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=sendrecv\r\n"
        branch = "z9hG4bK" + secrets.token_hex(8)
        await self.send(self.message("INVITE", sdp, branch=branch))
        reply = await self.answer()
        if reply.startswith(("SIP/2.0 401", "SIP/2.0 407")):
            await self.send(self.message("ACK", branch=branch))
            header = re.search(r"(?im)^(WWW-Authenticate|Proxy-Authenticate): Digest (.+)", reply)
            values = dict(re.findall(r'(\w+)="([^"]*)"', header[2]))
            realm, nonce = values["realm"], values["nonce"]

            def md5(value):
                return hashlib.md5(value.encode()).hexdigest()

            ha1 = md5(f"{self.account['username']}:{realm}:{self.account['password']}")
            ha2 = md5("INVITE:" + self.uri)
            pieces = f'username="{self.account["username"]}",realm="{realm}",nonce="{nonce}",uri="{self.uri}"'
            if values.get("qop"):
                cnonce = secrets.token_hex(8)
                response = md5(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}")
                pieces += f',qop=auth,nc=00000001,cnonce="{cnonce}"'
            else:
                response = md5(f"{ha1}:{nonce}:{ha2}")
            pieces += f',response="{response}"'
            auth = (
                ("Proxy-Authorization" if header[1].lower().startswith("proxy") else "Authorization")
                + ": Digest "
                + pieces
            )
            self.cseq += 1
            await self.send(self.message("INVITE", sdp, auth))
            reply = await self.answer()
        if not reply.startswith("SIP/2.0 200"):
            raise RuntimeError("SIP call failed: " + reply.splitlines()[0])
        tag = re.search(r"(?im)^To:.*;tag=([^;\s]+)", reply)
        self.to_tag = tag[1] if tag else ""
        media = re.search(r"(?m)^m=audio (\d+)", reply)
        address = re.search(r"(?m)^c=IN IP4 (\S+)", reply)
        self.remote_rtp = (address[1], int(media[1]))
        await self.send(self.message("ACK"))

    async def packet(self, payload, kind=0, marker=False, advance=160):
        header = struct.pack(
            "!BBHII", 0x80, kind | (128 if marker else 0), self.seq % 65536, self.stamp % (2**32), self.ssrc
        )
        await asyncio.get_running_loop().sock_sendto(self.rtp, header + payload, self.remote_rtp)
        self.seq += 1
        self.stamp += advance

    async def receive_audio(self):
        while True:
            data, _ = await asyncio.get_running_loop().sock_recvfrom(self.rtp, 65536)
            if len(data) >= 12 and data[1] & 127 == 0:
                self.audio.extend(audioop.ulaw2lin(data[12:], 2))

    async def silence(self, seconds):
        for _ in range(round(seconds * 50)):
            await self.packet(b"\xff" * 160)
            await asyncio.sleep(0.02)

    async def dtmf(self, digit):
        stamp = self.stamp
        for i in range(6):
            self.stamp = stamp
            ending = i >= 3
            await self.packet(
                struct.pack("!BBH", digit, 10 | (128 if ending else 0), min(i + 1, 4) * 160),
                101,
                marker=i == 0,
                advance=0,
            )
            await asyncio.sleep(0.02)
        self.stamp = stamp + 640

    async def speak(self, audio):
        for offset in range(0, len(audio), 320):
            frame = audio[offset : offset + 320].ljust(320, b"\0")
            await self.packet(audioop.lin2ulaw(frame, 2))
            await asyncio.sleep(0.02)

    async def close(self):
        self.cseq += 1
        with __import__("contextlib").suppress(Exception):
            await self.send(self.message("BYE"))
        self.sip.close()
        self.rtp.close()


async def main():
    metadata = json.loads((Path(settings.private_runtime_dir) / "sip/metadata.json").read_text())
    piper = PiperVoice.load(str(ROOT / settings.piper_model))
    chunks = list(piper.synthesize("Расскажите, пожалуйста, какой ваш адрес."))
    audio = np.concatenate(
        [np.frombuffer(c.audio_int16_bytes, "<i2").astype(np.float32) / 32768 for c in chunks]
    )
    pcm = (soxr.resample(audio, chunks[0].sample_rate, 8000) * 32767).astype("<i2").tobytes()
    output = ROOT / "artifacts/sip-acceptance"
    output.mkdir(exist_ok=True)
    results = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:7860", trust_env=False, timeout=10) as http:
        await http.post("/api/v1/operator/login", json={"password": settings.admin_password, "role": "admin"})
        initial = (await http.get("/api/v1/call-modes")).json()
        current = initial
        try:
            r = await http.put(
                "/api/v1/operator/call-settings", json={"mode": "sip", "revision": initial["revision"]}
            )
            r.raise_for_status()
            current = r.json()
            for profile, extension in metadata["extensions"].items():
                caller = SIPCaller(metadata["host"], metadata["port"], metadata["accounts"][0], extension)
                receiver = None
                try:
                    await caller.connect()
                    receiver = asyncio.create_task(caller.receive_audio())
                    await caller.silence(3)
                    await caller.dtmf(1)
                    await caller.silence(4)
                    await caller.speak(pcm)
                    await caller.silence(12)
                    with wave.open(str(output / f"{profile}.wav"), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(8000)
                        w.writeframes(caller.audio)
                    assert (
                        caller.audio
                        and np.max(np.abs(np.frombuffer(caller.audio, "<i2").astype(np.int32))) > 300
                    )
                    sessions = (await http.get("/api/v1/operator/sessions")).json()["items"]
                    recent = sessions[0]
                    detail = (await http.get("/api/v1/operator/sessions/" + recent["id"])).json()
                    turns = detail["turns"]
                    user = " ".join(t["text"] for t in turns if t["role"] == "user")
                    assistant = " ".join(t["text"] for t in turns if t["role"] == "assistant")
                    assert "адрес" in user.lower(), user
                    assert "Минск" in assistant, assistant
                    row = {
                        "profile": profile,
                        "sip_authenticated": True,
                        "dtmf_consent": True,
                        "user": user,
                        "assistant": assistant,
                        "received_audio_seconds": len(caller.audio) / 16000,
                    }
                    results.append(row)
                    print(row, flush=True)
                finally:
                    await caller.close()
                    if receiver:
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
                    await asyncio.sleep(1)
        finally:
            await http.put(
                "/api/v1/operator/call-settings",
                json={"mode": initial["mode"], "revision": current["revision"]},
            )
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
