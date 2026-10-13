"""Exercise reference-conditioned Russian speech, including final word endings."""

import json
import time
import wave
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.tts.utils import load_model

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "artifacts/voice-profile-validation"
OUTPUT.mkdir(parents=True, exist_ok=True)
metadata = json.loads((ROOT / "app/assets/voices/metadata.json").read_text())
model = load_model(str(ROOT / "models/qwen3-tts-base-0.6b"))
texts = [
    "Здравствуйте. Меня зовут {name}. Расскажите, пожалуйста, чем я могу вам помочь.",
    "Ваша запись на завтра подтверждена. Если ваши планы изменятся, запись можно перенести.",
    "Стоимость консультации составляет пятьдесят рублей. Оплата производится после консультации.",
]
measurements = []
for profile in ("male", "female"):
    reference = metadata[profile]
    for i, template in enumerate(texts):
        mx.random.seed(202610124 + i)
        text = template.format(name=reference["name"])
        started = time.perf_counter()
        chunks = []
        first = None
        for result in model.generate(
            text=text,
            ref_audio=str(ROOT / "app/assets/voices" / reference["file"]),
            ref_text=reference["text"],
            lang_code="russian",
            stream=True,
            streaming_interval=0.32,
            temperature=0.6,
            top_p=0.95,
            repetition_penalty=1.05,
            max_tokens=500,
            verbose=False,
        ):
            audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
            if first is None:
                first = time.perf_counter() - started
            chunks.append(audio)
        audio = np.concatenate(chunks)
        elapsed = time.perf_counter() - started
        if not np.isfinite(audio).all() or not audio.size:
            raise RuntimeError("Invalid speech")
        filename = f"{profile}-{i}.wav"
        with wave.open(str(OUTPUT / filename), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        row = {
            "profile": profile,
            "file": filename,
            "text": text,
            "first_audio_seconds": first,
            "elapsed_seconds": elapsed,
            "audio_seconds": len(audio) / 24000,
            "rtf": elapsed / (len(audio) / 24000),
        }
        measurements.append(row)
        print(profile, i, "first", round(first, 3), "rtf", round(row["rtf"], 3), flush=True)
        (OUTPUT / "metrics.json").write_text(json.dumps(measurements, ensure_ascii=False, indent=2) + "\n")
