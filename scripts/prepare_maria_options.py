"""Prepare synthetic female alternatives without changing either live voice.

Run the two stages in separate processes to release the design model before
loading the model used by calls. Previews use its normal streaming parameters.
"""

import argparse
import hashlib
import json
import time
import wave
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.tts.utils import load_model

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "artifacts/maria-options-20261013"
REFERENCE_TEXT = (
    "Здравствуйте. Меня зовут Мария. Я внимательно вас слушаю. "
    "Расскажите, пожалуйста, чем я могу помочь. "
    "Давайте вместе спокойно разберём ваш вопрос."
)
PREVIEW_TEXT = (
    "Скажите, пожалуйста, какое время вам удобно. "
    "Я проверю свободные часы и помогу выбрать подходящий вариант."
)
OPTIONS = [
    {
        "id": "velvet",
        "label": "Низкий бархатный",
        "description": (
            "A native Russian woman aged around forty with a distinctly low contralto "
            "speaking voice. Rich dark velvet timbre, smooth chest resonance, gentle "
            "and reassuring. Clear clean vocal tone, unforced normal speaking volume, "
            "not whispered or breathy. Natural Russian conversational rhythm, precise "
            "diction, clear vowels and complete word endings."
        ),
    },
    {
        "id": "clear",
        "label": "Спокойный средний",
        "description": (
            "A native Russian woman in her thirties with a plain, clear everyday "
            "speaking voice in a medium register. Direct, steady, composed and "
            "attentive, like a friendly professional talking naturally to one person. "
            "Clean focused vocal resonance, firm clear articulation, ordinary "
            "conversational pace, little breathiness, no singing or theatrical delivery. "
            "Fluent standard Russian and complete word endings."
        ),
    },
    {
        "id": "lively",
        "label": "Живой и тёплый",
        "description": (
            "A native Russian adult woman with a warm expressive alto voice. "
            "Lightly smiling, lively and engaging conversational intonation, relaxed "
            "and approachable. Rounded full-bodied resonance with a comfortable "
            "medium-low pitch, never shrill or squeaky. Clear flowing Russian speech, "
            "natural sentence melody, precise consonants and complete word endings. "
            "Normal speaking volume and moderate pace."
        ),
    },
]


def fingerprint_live_voices():
    return {
        name: hashlib.sha256((ROOT / "app/assets/voices" / name).read_bytes()).hexdigest()
        for name in ("male.wav", "female.wav", "metadata.json")
    }


def write_audio(path, chunks, sample_rate=24000):
    if not chunks:
        raise RuntimeError(f"No audio: {path.name}")
    audio = np.concatenate(chunks)
    seconds = audio.size / sample_rate
    if not np.isfinite(audio).all() or not 2 < seconds < 30:
        raise RuntimeError(f"Invalid or excessive audio: {path.name}, {seconds:.2f}s")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return seconds


def save_metadata(rows):
    path = OUTPUT / "metadata.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def references():
    model = load_model(str(ROOT / "models/qwen3-voice-design-1.7b"))
    rows = []
    for i, option in enumerate(OPTIONS, 1):
        filename = f"{option['id']}-reference.wav"
        for attempt in range(3):
            seed = 202610130 + i + (attempt + 1) * 100
            mx.random.seed(seed)
            started = time.perf_counter()
            results = list(
                model.generate_voice_design(
                    text=REFERENCE_TEXT,
                    language="russian",
                    instruct=option["description"],
                    temperature=0.6,
                    top_p=0.95,
                    repetition_penalty=1.05,
                    max_tokens=360,
                    stream=False,
                    verbose=False,
                )
            )
            chunks = [np.asarray(result.audio, dtype=np.float32).reshape(-1) for result in results]
            if any(result.token_count >= 360 for result in results):
                write_audio(OUTPUT / f"{option['id']}-rejected-{attempt}.wav", chunks)
                print(option["id"], "rejected: token limit; trying new seed", flush=True)
                continue
            seconds = write_audio(OUTPUT / filename, chunks)
            break
        else:
            print(option["id"], "no usable reference", flush=True)
            continue
        rows.append(
            {
                **option,
                "seed": seed,
                "reference_file": filename,
                "reference_text": REFERENCE_TEXT,
                "reference_seconds": seconds,
                "source": "Qwen3-TTS-1.7B-VoiceDesign synthetic generation",
                "revision": "5c390979e4b93af5f2932f90742ca99c7dd04687",
            }
        )
        save_metadata(rows)
        print(option["id"], "reference", round(seconds, 2), "seconds; generated in",
              round(time.perf_counter() - started, 2), flush=True)
    if len(rows) != len(OPTIONS):
        raise RuntimeError("Not all voice alternatives produced usable references")


def previews():
    rows = json.loads((OUTPUT / "metadata.json").read_text())
    model = load_model(str(ROOT / "models/qwen3-tts-base-0.6b"))
    for row in rows:
        mx.random.seed(row["seed"] + 10)
        started = time.perf_counter()
        first = None
        chunks = []
        tokens = 0
        for result in model.generate(
            text=PREVIEW_TEXT,
            ref_audio=str(OUTPUT / row["reference_file"]),
            ref_text=row["reference_text"],
            lang_code="russian",
            stream=True,
            streaming_interval=0.32,
            temperature=0.6,
            top_p=0.95,
            repetition_penalty=1.05,
            max_tokens=360,
            verbose=False,
        ):
            if first is None:
                first = time.perf_counter() - started
            tokens += result.token_count
            chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
        if tokens >= 360:
            raise RuntimeError(f"Preview reached its token limit: {row['id']}")
        filename = f"{row['id']}-preview.wav"
        seconds = write_audio(OUTPUT / filename, chunks)
        row.update(
            preview_file=filename,
            preview_text=PREVIEW_TEXT,
            preview_seconds=seconds,
            preview_first_seconds=first,
            preview_generation_seconds=time.perf_counter() - started,
        )
        save_metadata(rows)
        print(row["id"], "call preview", round(seconds, 2), "seconds; generated in",
              round(row["preview_generation_seconds"], 2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("references", "previews"))
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    before = fingerprint_live_voices()
    {"references": references, "previews": previews}[args.stage]()
    if fingerprint_live_voices() != before:
        raise RuntimeError("Live voice assets changed during candidate preparation")
    print("Live voice assets unchanged", flush=True)
