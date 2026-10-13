"""Generate replacement synthetic voices without changing the accepted male reference."""

import json
import time
import wave
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.tts.utils import load_model

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "artifacts/maria-candidates"
OUTPUT.mkdir(parents=True, exist_ok=True)
TEXT = (
    "Здравствуйте. Меня зовут Мария. Я помогу вам выбрать удобное время для записи. "
    "Скажите, пожалуйста, какая услуга вас интересует. Мы проверим свободные часы, "
    "уточним стоимость и подтвердим все детали. Если ваши планы изменятся, запись можно перенести."
)
DESCRIPTIONS = [
    "A Russian woman in her thirties, a warm natural speaking voice, medium-low pitch. "
    "She speaks fluent native Russian with clear crisp articulation and complete word endings. "
    "Relaxed, friendly, conversational, confident, normal speaking volume, smooth even pacing.",
    "A mature Russian female receptionist, naturally warm and composed. Her voice has a "
    "rounded mezzo-soprano timbre and clear resonance. Native standard Russian pronunciation, "
    "precise consonants, every syllable clearly spoken, lively friendly conversational intonation.",
    "A native Russian woman with a clear everyday voice in a comfortable middle register. "
    "Friendly, straightforward, calm and attentive. Excellent Russian diction, full word endings, "
    "natural flowing sentences, moderate speaking pace and confident clear tone.",
]
model = load_model(str(ROOT / "models/qwen3-voice-design-1.7b"))
metadata = []
for i, description in enumerate(DESCRIPTIONS, 1):
    mx.random.seed(202610120 + i)
    started = time.perf_counter()
    results = list(
        model.generate_voice_design(
            text=TEXT,
            language="russian",
            instruct=description,
            temperature=0.55,
            top_p=0.9,
            repetition_penalty=1.05,
            max_tokens=700,
            stream=False,
            verbose=False,
        )
    )
    audio = np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in results])
    if not np.isfinite(audio).all() or audio.size < 24000:
        raise RuntimeError("Invalid generated reference")
    filename = f"maria-{i}.wav"
    with wave.open(str(OUTPUT / filename), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    metadata.append(
        {
            "file": filename,
            "text": TEXT,
            "description": description,
            "seconds": audio.size / 24000,
            "seed": 202610120 + i,
        }
    )
    print(
        filename,
        round(audio.size / 24000, 2),
        "seconds; generation",
        round(time.perf_counter() - started, 2),
        flush=True,
    )
    (OUTPUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
