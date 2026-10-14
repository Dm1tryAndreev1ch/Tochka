"""Compare approved voices' first PCM, throughput and opt-in cache, without an LLM."""

import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.natural_tts import Engine  # noqa: E402

TEXTS = [
    "Консультация стоит тридцать белорусских рублей. Хотите записаться?",
    "Перед консультацией сформулируйте основной вопрос и подготовьте документы.",
]


async def measure(engine, text, profile, interval, output, cache=False):
    started = time.perf_counter()
    response = await engine.stream(text, None, profile=profile, cache=cache)
    first = None
    audio = bytearray()
    async for chunk in response.body_iterator:
        if chunk and first is None:
            first = time.perf_counter() - started
        audio.extend(chunk)
    elapsed = time.perf_counter() - started
    duration = len(audio) / 48000
    if not duration:
        raise RuntimeError("No synthesized audio")
    with wave.open(str(output), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(24000)
        file.writeframes(audio)
    return {
        "profile": profile, "interval": interval, "cache": cache,
        "text": text, "first_pcm_seconds": first, "elapsed_seconds": elapsed,
        "audio_seconds": duration, "rtf": elapsed / duration, "audio_file": str(output),
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/latency-tts-20261013")
    parser.add_argument("--intervals", nargs="+", type=float, default=[0.32, 0.16, 0.08])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((ROOT / settings.voice_reference_dir / "metadata.json").read_text())
    report = {
        "scope": "isolated warm approved-voice synthesis; first server PCM, not audible end-to-end latency",
        "platform": platform.platform(), "model": settings.natural_tts_model,
        "male_rate": settings.male_speech_rate,
        "reference_sha256": {
            p: hashlib.sha256((ROOT / settings.voice_reference_dir / metadata[p]["file"]).read_bytes()).hexdigest()
            for p in ("male", "female")
        },
        "rows": [], "summary": {},
    }
    engine = Engine()
    try:
        await asyncio.to_thread(engine.load)
        if not engine.ready:
            raise RuntimeError(engine.error)
        for interval in args.intervals:
            if interval <= 0:
                raise ValueError("Interval must be positive")
            settings.tts_streaming_interval = interval
            for profile in ("male", "female"):
                for index, text in enumerate(TEXTS):
                    row = await measure(engine, text, profile, interval, args.output / f"{profile}-{interval}-{index}.wav", cache=True)
                    report["rows"].append(row)
                    repeated = await measure(engine, text, profile, interval, args.output / f"{profile}-{interval}-{index}-cache.wav", cache=True)
                    report["rows"].append(repeated)
                    # Mark the first call as a cache miss, not a hit.
                    row["cache_hit"] = False
                    repeated["cache_hit"] = True
                    print(json.dumps({k: row[k] for k in ("profile", "interval", "first_pcm_seconds", "rtf")}), flush=True)
                    engine.audio_cache.clear()
            rows = [r for r in report["rows"] if r["interval"] == interval and not r["cache_hit"]]
            report["summary"][str(interval)] = {
                "samples": len(rows),
                "median_first_pcm_seconds": statistics.median(r["first_pcm_seconds"] for r in rows),
                "max_first_pcm_seconds": max(r["first_pcm_seconds"] for r in rows),
                "median_rtf": statistics.median(r["rtf"] for r in rows),
                "max_rtf": max(r["rtf"] for r in rows),
            }
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        for stop in engine.cancel_events:
            stop.set()
        if engine.cleanup_tasks:
            await asyncio.gather(*engine.cleanup_tasks, return_exceptions=True)
        await asyncio.to_thread(engine.executor.shutdown, wait=True, cancel_futures=True)
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
