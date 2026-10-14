"""Reproducible ASR diagnostics on licensed public human speech; errors remain in scores."""

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from scipy.signal import butter, sosfilt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.audio_tempo import SpeechTempo  # noqa: E402
from app.models import LocalModels  # noqa: E402

PROFILES = ("clean", "phone", "street_simulated", "office_simulated", "echo", "fast", "pauses", "combined")
SPEC = {
    "version": 1,
    "noise_snr_db": 15,
    "echo_delay_seconds": 0.12,
    "echo_gain": 0.15,
    "speed": 1.15,
    "pause_seconds": 0.35,
    "phone_band_hz": [300, 3400],
    "phone_rate": 8000,
    "note": "Controlled acoustic simulations on real public speech; not physical device/environment acceptance",
}


def words(text):
    return re.findall(r"[а-яa-z0-9]+", text.casefold().replace("ё", "е"))


def errors(reference, hypothesis):
    a, b = words(reference), words(hypothesis)
    previous = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1], len(a)


def phone(audio):
    band = sosfilt(butter(4, [300, 3400], fs=16000, btype="bandpass", output="sos"), audio)
    narrow = soxr.resample(band, 16000, 8000)
    # G.711-style logarithmic quantization without a deprecated audioop dependency.
    quant = np.sign(narrow) * np.log1p(255 * np.abs(narrow)) / np.log(256)
    quant = np.round((quant + 1) * 127.5) / 127.5 - 1
    decoded = np.sign(quant) * np.expm1(np.abs(quant) * np.log(256)) / 255
    return soxr.resample(decoded, 8000, 16000).astype(np.float32)


def speed(audio):
    tempo = SpeechTempo(rate=SPEC["speed"], sample_rate=16000)
    try:
        return np.concatenate([tempo.push(audio), tempo.finish()])
    finally:
        tempo.close()


def transform(audio, profile, seed, babble):
    rng = np.random.default_rng(seed)
    result = audio.copy()
    if profile in ("fast", "combined"):
        result = speed(result)
    if profile in ("pauses", "combined"):
        # Extend low-energy gaps, avoiding arbitrary cuts through spoken words.
        pieces = []
        start = 0
        for target in range(3 * 16000, len(result) - 16000, 5 * 16000):
            lo = max(start, target - 16000 // 2)
            hi = min(len(result), target + 16000 // 2)
            candidates = range(lo, hi, 320)
            at = min(candidates, key=lambda p: np.mean(result[p : p + 320] ** 2))
            if np.mean(result[at : at + 320] ** 2) < 0.0003:
                pieces.extend((result[start:at], np.zeros(int(0.35 * 16000), dtype=np.float32)))
                start = at
        pieces.append(result[start:])
        result = np.concatenate(pieces)
    if profile in ("echo", "combined"):
        offset = int(0.12 * 16000)
        echo = np.zeros(len(result) + offset, dtype=np.float32)
        echo[: len(result)] += result
        echo[offset:] += result * 0.15
        result = echo
    if profile in ("street_simulated", "office_simulated", "combined"):
        if profile == "office_simulated":
            noise = np.resize(babble, len(result)).astype(np.float32)
        else:
            noise = sosfilt(butter(2, 1200, fs=16000, output="sos"), rng.normal(size=len(result))).astype(
                np.float32
            )
        speech_rms = np.sqrt(np.mean(result**2))
        noise_rms = np.sqrt(np.mean(noise**2))
        result = result + noise * (speech_rms / max(noise_rms, 1e-8) / 10 ** (15 / 20))
    if profile in ("phone", "combined"):
        result = phone(result)
    return np.clip(result, -1, 1).astype(np.float32)


def read(path):
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return soxr.resample(audio, sr, 16000).astype(np.float32) if sr != 16000 else audio


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest", type=Path, default=Path.home() / ".cache/voice-desk/evaluation/minds14-ru/manifest.json"
    )
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--limit", type=int, default=14)
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=list(PROFILES))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/asr-public-dev.json")
    parser.add_argument("--backend", choices=("production", "gigaam-rnnt"), default="production")
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()
    corpus = json.loads(args.manifest.read_text())
    records = [r for r in corpus["records"] if r["split"] == args.split]
    # Interleave labels so a small development run covers the domains instead of only the first classes.
    grouped = {label: [r for r in records if r["intent"] == label] for label in corpus["labels"]}
    ordered = []
    if grouped:
        for i in range(max(map(len, grouped.values()))):
            ordered.extend(rows[i] for rows in grouped.values() if i < len(rows))
    else:
        ordered = records
    records = ordered[: args.limit]
    if args.backend == "production":
        models = LocalModels()
        models.load()
        if not models.ready:
            raise RuntimeError(models.error)
        def transcribe(signal):
            return models.transcribe((signal * 32767).astype(np.int16).tobytes())
        model_info = {"backend": "production"}
    else:
        if not args.model_dir or not args.model_dir.is_dir():
            raise ValueError("Candidate evaluation requires a local, pinned model directory")
        import mlx.core as mx
        from gigaam_mlx import load_model
        from gigaam_mlx.audio import compute_mel, split_audio

        model, tokenizer = load_model("rnnt", repo_id=str(args.model_dir))

        def transcribe(signal):
            parts = []
            for chunk in split_audio(signal):
                audio_part = signal[chunk["start_sample"]:chunk["end_sample"]]
                mel = mx.array(compute_mel(audio_part)[np.newaxis])
                encoded, length = model.encode(mel)
                mx.eval(encoded)
                parts.append(tokenizer.decode(model.decode(encoded, length)))
            return " ".join(parts)

        model_info = {"backend": args.backend, "files": {
            name: hashlib.sha256((args.model_dir / name).read_bytes()).hexdigest()
            for name in ("weights.safetensors", "tokenizer.model")
        }}
    rows = []
    babble = read(corpus["records"][-1]["audio"])
    report = {
        "scope": "public human-speech ASR diagnostic; no product NLU acceptance",
        "model": model_info,
        "dataset": corpus["dataset"],
        "revision": corpus["revision"],
        "source": corpus["source"],
        "license": corpus["license"],
        "split": args.split,
        "speaker_split_verified": corpus["speaker_split_verified"],
        "profiles": SPEC,
        "results": rows,
    }
    try:
        for record in records:
            if hashlib.sha256(Path(record["audio"]).read_bytes()).hexdigest() != record["sha256"]:
                raise RuntimeError("Corpus checksum mismatch")
            audio = read(record["audio"])
            for profile in args.profiles:
                signal = transform(audio, profile, int(hashlib.sha256(record["id"].encode()).hexdigest()[:8], 16), babble)
                started = time.monotonic()
                try:
                    hypothesis = await asyncio.to_thread(transcribe, signal)
                    failure = None
                except Exception as exc:
                    hypothesis = ""
                    failure = type(exc).__name__
                distance, count = errors(record["text"], hypothesis)
                rows.append(
                    {
                        "id": record["id"],
                        "profile": profile,
                        "reference": record["text"],
                        "hypothesis": hypothesis,
                        "errors": distance,
                        "words": count,
                        "seconds": round(time.monotonic() - started, 3),
                        "failure": failure,
                    }
                )
                report["aggregate"] = {
                    p: {
                        "words": sum(x["words"] for x in rows if x["profile"] == p),
                        "errors": sum(x["errors"] for x in rows if x["profile"] == p),
                    }
                    for p in args.profiles
                }
                for value in report["aggregate"].values():
                    value["word_accuracy"] = (
                        100 * (1 - value["errors"] / value["words"]) if value["words"] else None
                    )
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(len(rows), profile, "errors", distance, "/", count, flush=True)
    finally:
        if args.backend == "production":
            await models.close()
    print(json.dumps(report["aggregate"], ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
