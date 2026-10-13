import json
import sys
import time
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402

texts = [
    "Здравствуйте! Я виртуальный помощник. Чем могу помочь?",
    "Консультация стоит тридцать белорусских рублей и длится тридцать минут.",
    "Вы записаны на тринадцатое октября в одиннадцать часов. Сохраните ссылку управления.",
]
records = []
with httpx.Client(
    base_url=settings.natural_tts_url,
    headers={"Authorization": "Bearer " + settings.natural_tts_api_key},
    timeout=60,
    trust_env=False,
) as client:
    for i, text in enumerate(texts):
        started = time.perf_counter()
        first = None
        audio = []
        with client.stream("POST", "/synthesize", json={"text": text}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if first is None:
                    first = time.perf_counter() - started
                audio.append(chunk)
        elapsed = time.perf_counter() - started
        pcm = b"".join(audio)
        duration = len(pcm) / 48000
        with wave.open(str(ROOT / f"artifacts/natural-ryan-{i}.wav"), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(pcm)
        result = {
            "text": text,
            "first_audio_seconds": first,
            "elapsed": elapsed,
            "audio_seconds": duration,
            "rtf": elapsed / duration,
        }
        records.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        (ROOT / "artifacts/natural-tts-benchmark.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2)
        )
