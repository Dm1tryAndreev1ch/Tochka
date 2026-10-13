import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import soxr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.models import LocalModels  # noqa: E402

TEXTS = [
    "Сколько стоит консультация?",
    "Хочу записаться на диагностику завтра в одиннадцать.",
    "Нет, лучше в двенадцать. Меня зовут Александр.",
    "Здравствуйте! Я виртуальный помощник. Чем могу помочь?",
    "Консультация стоит тридцать белорусских рублей и длится тридцать минут.",
    "Вы записаны на тринадцатое октября в одиннадцать часов. Сохраните ссылку управления.",
]


async def main():
    model = LocalModels()
    model.load()
    if not model.ready:
        raise RuntimeError(model.error)
    records = []
    for i, text in enumerate(TEXTS):
        started = time.perf_counter()
        chunks = model.synthesize(text)
        elapsed = time.perf_counter() - started
        audio = b"".join(c[0] for c in chunks)
        rate = chunks[0][1]
        duration = len(audio) / (rate * 2)
        path = ROOT / f"artifacts/voice-baseline-{i}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(audio)
        pcm = (
            (
                soxr.resample(np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768, rate, 16000)
                * 32768
            )
            .clip(-32768, 32767)
            .astype(np.int16)
            .tobytes()
        )
        started = time.perf_counter()
        transcript = model.transcribe(pcm)
        stt_seconds = time.perf_counter() - started
        r = {
            "text": text,
            "transcript": transcript,
            "duration": duration,
            "tts_seconds": elapsed,
            "tts_first_audio_seconds": elapsed,
            "tts_rtf": elapsed / duration,
            "stt_seconds": stt_seconds,
            "stt_rtf": stt_seconds / duration,
        }
        records.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        (ROOT / "artifacts/baseline-components.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2)
        )
    await model.close()


if __name__ == "__main__":
    asyncio.run(main())
