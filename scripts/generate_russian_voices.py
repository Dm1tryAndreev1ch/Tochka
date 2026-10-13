"""Create synthetic Russian personas; no real person's reference recording is used."""

import json
import time
import wave
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.tts.utils import load_model

ROOT = Path(__file__).resolve().parent.parent
path = ROOT / "app/assets/voices"
path.mkdir(parents=True, exist_ok=True)
model = load_model(str(ROOT / "models/qwen3-voice-design-1.7b"))
profiles = {
    "male": {
        "name": "Геннадий",
        "text": "Здравствуйте! Меня зовут Геннадий. Я ваш виртуальный помощник. Давайте спокойно разберём ваш вопрос и выберем удобное время для записи.",
        "description": "A native Russian man speaking standard Russian with no foreign accent. His voice is soft, calm and friendly, with a low warm baritone, smooth chest resonance, and gentle conversational pacing. Mature adult male, not theatrical, not gravelly, not overly deep. Clear Russian vowels and consonants.",
    },
    "female": {
        "name": "Мария",
        "text": "Здравствуйте! Меня зовут Мария. Я ваш виртуальный помощник. Расскажите, чем я могу помочь. Вместе найдём удобное время и проверим все детали вашей записи.",
        "description": "A native Russian adult woman speaking clear standard Russian with absolutely no foreign accent. A warm, gentle, reassuring voice in a comfortable lower middle register. Not high pitched, not squeaky, not childish. Natural calm conversational intonation, clear diction, soft but confident.",
    },
}
metadata = {}
for profile, data in profiles.items():
    mx.random.seed(20261012 + (profile == "female"))
    started = time.perf_counter()
    results = list(
        model.generate_voice_design(
            text=data["text"],
            language="russian",
            instruct=data["description"],
            temperature=0.65,
            top_p=0.95,
            repetition_penalty=1.1,
            max_tokens=600,
            stream=False,
            verbose=False,
        )
    )
    audio = np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in results])
    if not np.isfinite(audio).all() or audio.size < 24000:
        raise RuntimeError("Invalid generated reference")
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    filename = profile + ".wav"
    with wave.open(str(path / filename), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(pcm)
    metadata[profile] = {
        **data,
        "file": filename,
        "source": "Qwen3-TTS-1.7B-VoiceDesign synthetic generation",
        "revision": "5c390979e4b93af5f2932f90742ca99c7dd04687",
        "seconds": audio.size / 24000,
    }
    (path / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print(
        profile,
        "audio seconds",
        round(audio.size / 24000, 2),
        "generation",
        round(time.perf_counter() - started, 2),
        flush=True,
    )
del model
mx.clear_cache()
