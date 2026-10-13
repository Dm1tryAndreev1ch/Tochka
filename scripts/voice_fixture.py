import wave
from pathlib import Path

from piper import PiperVoice

root = Path(__file__).resolve().parent.parent
voice = PiperVoice.load(str(root / "models/ru_RU-denis-medium.onnx"))
chunks = list(voice.synthesize("Сколько стоит консультация?"))
with wave.open(str(root / "artifacts/voice-test-input.wav"), "wb") as file:
    file.setnchannels(1)
    file.setsampwidth(2)
    file.setframerate(22050)
    file.writeframes(
        bytes(22050 * 2 * 8) + b"".join(c.audio_int16_bytes for c in chunks) + bytes(22050 * 2 * 45)
    )
print("Generated Russian audio fixture for WebRTC browser tests")
