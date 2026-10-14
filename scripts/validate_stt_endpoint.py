"""Compare real Whisper decoding of early/final PCM and detect premature VAD stops.

The licensed human recordings and Piper synthetic cases are acoustic diagnostics,
not physical microphone/device acceptance. Reference text is only scored; it is
never substituted for an ASR hypothesis. --prepare-only uses CPU and never loads
Whisper, so expensive GPU runs can be scheduled separately.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams, VADState  # noqa: E402

from app.config import settings  # noqa: E402
from app.stt_native import MetalWhisper  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

RATE = 16000
FRAME_SAMPLES = 512  # Silero's production window at 16 kHz.
TRAILING_SAMPLES = int(0.3 * RATE)
SYNTHETIC_CASES = (
    {"id": "name", "kind": "name", "parts": ["Меня зовут Анна Петрова."]},
    {"id": "name-correction", "kind": "correction", "parts": ["Меня зовут Иван Петров.", "Нет, Алексей Петров."]},
    {"id": "phone", "kind": "phone", "parts": ["Мой телефон, плюс триста семьдесят пять.", "Двадцать девять, сто двадцать три, сорок пять, шестьдесят семь."], "expected_digits": "375291234567"},
    {"id": "phone-correction", "kind": "phone", "parts": ["Последние цифры номера, сорок пять, шестьдесят семь.", "Нет, исправьте, сорок пять, шестьдесят восемь."], "expected_digits": "4568"},
    {"id": "date", "kind": "date", "parts": ["Запишите меня на двадцать третье октября.", "Две тысячи двадцать шестого года."], "expected_digits": "23102026"},
    {"id": "time", "kind": "time", "parts": ["Мне удобно завтра в четырнадцать часов.", "Тридцать минут."], "expected_digits": "1430"},
    {"id": "consent-correction", "kind": "consent", "parts": ["Да, согласен.", "Нет, не разрешаю записывать разговор."]},
    {"id": "faq", "kind": "ordinary", "parts": ["Сколько стоит консультация и где находится ваш офис?"]},
)


def normalized(text):
    return re.findall(r"[а-яa-z0-9]+", text.casefold().replace("ё", "е"))


def digits(text):
    return "".join(re.findall(r"\d+", text))


def word_distance(reference, hypothesis):
    a, b = normalized(reference), normalized(hypothesis)
    previous = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1], len(a)


def read_audio(path):
    signal, rate = sf.read(path, dtype="float32")
    if signal.ndim > 1:
        signal = signal.mean(axis=1)
    if rate != RATE:
        signal = soxr.resample(signal, rate, RATE)
    return (np.clip(signal, -1, 1) * 32767).astype("<i2")


def trim_generated_silence(signal):
    active = np.flatnonzero(np.abs(signal) > max(0.0005, float(np.max(np.abs(signal))) * 0.01))
    if not len(active):
        raise RuntimeError("Piper produced no speech")
    margin = int(0.03 * RATE)
    return signal[max(0, active[0] - margin) : min(len(signal), active[-1] + margin + 1)]


def prepare(args):
    from piper import PiperVoice

    args.input_dir.mkdir(parents=True, exist_ok=True)
    piper_path = ROOT / settings.piper_model
    voice = PiperVoice.load(str(piper_path))
    rows = []
    for case in SYNTHETIC_CASES:
        parts = []
        for part in case["parts"]:
            chunks = list(voice.synthesize(part))
            audio = np.concatenate(
                [np.frombuffer(chunk.audio_int16_bytes, dtype="<i2").astype(np.float32) / 32768 for chunk in chunks]
            )
            parts.append(trim_generated_silence(soxr.resample(audio, chunks[0].sample_rate, RATE)))
        for pause in args.pauses if len(parts) > 1 else [0.0]:
            pieces, gaps, offset = [], [], 0
            for index, part in enumerate(parts):
                if index:
                    gap_samples = int(pause * RATE)
                    gaps.append({"start": offset / RATE, "end": (offset + gap_samples) / RATE})
                    pieces.append(np.zeros(gap_samples, dtype=np.float32))
                    offset += gap_samples
                pieces.append(part)
                offset += len(part)
            signal = np.concatenate(pieces)
            case_id = case["id"] + (f"-pause-{pause:.2f}" if pause else "")
            path = args.input_dir / (case_id + ".wav")
            sf.write(path, signal, RATE, subtype="PCM_16")
            rows.append(
                {
                    "id": case_id,
                    "audio": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "kind": case["kind"],
                    "source": "Piper ru_RU-denis-medium synthetic speech",
                    "text": " ".join(case["parts"]),
                    "expected_digits": case.get("expected_digits"),
                    "injected_gaps": gaps,
                    "seconds": round(len(signal) / RATE, 3),
                }
            )
    manifest = json.loads(args.manifest.read_text())
    groups = {gender: [] for gender in ("MALE", "FEMALE", None)}
    for record in manifest["records"]:
        duration = sf.info(record["audio"]).duration
        if 2 <= duration <= 18:
            groups.setdefault(record.get("gender"), []).append(record)
    selected = []
    while len(selected) < args.human_limit and any(groups.values()):
        for records in groups.values():
            if records and len(selected) < args.human_limit:
                selected.append(records.pop(0))
    for record in selected:
        path = Path(record["audio"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError("Public corpus checksum mismatch")
        rows.append({**record, "id": record["id"] + "-" + record["sha256"][:8], "source_record_id": record["id"], "kind": "human", "injected_gaps": [], "seconds": round(sf.info(path).duration, 3)})
    inputs = {
        "version": 1,
        "records": rows,
        "synthetic_voice_sha256": hashlib.sha256(piper_path.read_bytes()).hexdigest(),
        "public_dataset": {key: manifest.get(key) for key in ("dataset", "revision", "license", "source", "speaker_split_verified")},
        "note": "Synthetic critical cases plus public human read speech; ASR errors retained, no transcript substitution",
    }
    (args.input_dir / "manifest.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2) + "\n")
    return inputs


@dataclass
class Segment:
    start: float
    end: float
    final: np.ndarray
    early: np.ndarray | None
    early_end: float | None
    discarded_early: int


async def endpoint_segments(signal, stop_seconds, early_seconds):
    """Mirror the production 16 kHz buffering/event ordering, without decoding."""
    analyzer = SileroVADAnalyzer(
        sample_rate=RATE,
        params=VADParams(confidence=0.65, start_secs=0.15, stop_secs=stop_seconds, min_volume=0.3),
    )
    analyzer.set_sample_rate(RATE)
    buffer = np.empty(0, dtype="<i2")
    speaking, controller_state = False, VADState.QUIET
    samples, quiet, start = 0, 0, 0.0
    early, early_end, discarded = None, None, 0
    output = []
    padded = np.concatenate([signal, np.zeros(int(1.5 * RATE), dtype="<i2")])
    try:
        for offset in range(0, len(padded), FRAME_SAMPLES):
            chunk = padded[offset : offset + FRAME_SAMPLES]
            if len(chunk) < FRAME_SAMPLES:
                chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
            buffer = np.concatenate([buffer, chunk])
            if speaking:
                samples += len(chunk)
            else:
                buffer = buffer[-RATE:]
            # Silero is CPU-only. Use its public API; await keeps model state
            # and the ordered activity observation bound to this exact frame.
            state = await analyzer.analyze_audio(chunk.tobytes())
            now = (offset + len(chunk)) / RATE
            if state == VADState.SPEAKING and controller_state == VADState.QUIET:
                speaking, controller_state = True, VADState.SPEAKING
                samples, quiet = 0, 0
                early, early_end, discarded = None, None, 0
                start = max(0.0, now - len(buffer) / RATE)
            elif state == VADState.QUIET and controller_state == VADState.SPEAKING:
                final = np.concatenate([buffer, np.zeros(TRAILING_SAMPLES, dtype="<i2")])
                output.append(Segment(start, now, final, early, early_end, discarded))
                speaking, controller_state = False, VADState.QUIET
                buffer = np.empty(0, dtype="<i2")
            if state in (VADState.STARTING, VADState.SPEAKING):
                quiet = 0
                if early is not None:
                    discarded += 1
                early, early_end = None, None
            elif speaking:
                quiet += len(chunk)
                if quiet >= RATE * early_seconds and samples >= RATE * 0.4 and early is None:
                    early = np.concatenate([buffer, np.zeros(TRAILING_SAMPLES, dtype="<i2")])
                    early_end = now
    finally:
        await analyzer.cleanup()
    return output


def checkpoint(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


async def evaluate(args, inputs):
    whisper_path = ROOT / settings.whisper_metal_model
    model = MetalWhisper(whisper_path)
    model.transcribe(np.zeros(RATE, dtype=np.float32))
    rows, decode_cache = [], {}
    report = {
        "version": 1,
        "scope": "Offline acoustic safety gate for VAD endpoint and early STT; no microphone/network/browser acceptance",
        "model": {"path": str(whisper_path), "sha256": hashlib.sha256(whisper_path.read_bytes()).hexdigest(), "version": model.version, "backend": "whisper.cpp Metal"},
        "inputs_manifest": str(args.input_dir / "manifest.json"),
        "public_dataset": inputs["public_dataset"],
        "configuration": {"stop_seconds": args.stop_seconds, "early_silence_seconds": args.early_seconds, "start_seconds": 0.15, "trailing_silence_seconds": 0.3, "sample_rate": RATE, "frame_samples": FRAME_SAMPLES},
        "results": rows,
    }

    async def decode(pcm):
        key = hashlib.sha256(pcm.tobytes()).hexdigest()
        if key not in decode_cache:
            started = time.perf_counter()
            try:
                text = await asyncio.to_thread(model.transcribe, pcm.astype(np.float32) / 32768)
                failure = None
            except Exception as error:
                text, failure = "", type(error).__name__ + ": " + str(error)
            decode_cache[key] = {"text": text, "seconds": round(time.perf_counter() - started, 4), "pcm_sha256": key, "failure": failure}
        return decode_cache[key]

    try:
        for record in inputs["records"]:
            if hashlib.sha256(Path(record["audio"]).read_bytes()).hexdigest() != record["sha256"]:
                raise RuntimeError("Evaluation input checksum mismatch: " + record["id"])
            signal = read_audio(record["audio"])
            for stop_seconds in args.stop_seconds:
                segments = await endpoint_segments(signal, stop_seconds, args.early_seconds)
                result = {"id": record["id"], "kind": record["kind"], "reference": record["text"], "audio": record["audio"], "audio_sha256": record["sha256"], "stop_seconds": stop_seconds, "segments": []}
                for index, segment in enumerate(segments):
                    final = await decode(segment.final)
                    early = await decode(segment.early) if segment.early is not None else None
                    entry = {
                        "index": index,
                        "start": round(segment.start, 3), "end": round(segment.end, 3),
                        "early_end": round(segment.early_end, 3) if segment.early_end is not None else None,
                        "available_overlap_seconds": round(segment.end - segment.early_end, 3) if segment.early_end is not None else 0,
                        "discarded_early_snapshots": segment.discarded_early,
                        "final": final, "early": early,
                        "normalized_parity": normalized(early["text"]) == normalized(final["text"]) if early else None,
                        "last_three_tokens_parity": normalized(early["text"])[-3:] == normalized(final["text"])[-3:] if early else None,
                        "digit_parity": digits(early["text"]) == digits(final["text"]) if early else None,
                    }
                    result["segments"].append(entry)
                    if early and not entry["normalized_parity"]:
                        diagnostic = args.input_dir / "mismatches"
                        diagnostic.mkdir(exist_ok=True)
                        for variant, pcm in (("early", segment.early), ("final", segment.final)):
                            path = diagnostic / f"{record['id']}-{stop_seconds:.2f}-{index}-{variant}.wav"
                            sf.write(path, pcm, RATE, subtype="PCM_16")
                            entry[variant + "_diagnostic_audio"] = str(path)
                combined = " ".join(segment["final"]["text"] for segment in result["segments"])
                distance, count = word_distance(record["text"], combined)
                result.update(final_combined=combined, reference_word_errors=distance, reference_words=count)
                result["observed_digits"] = digits(combined)
                result["expected_digits"] = record.get("expected_digits")
                result["reference_digit_match"] = record["expected_digits"] in digits(combined) if record.get("expected_digits") else None
                # Extra VAD stops inside a known recording are observable. A
                # .95 baseline can itself split long human read speech, so gate
                # on additional segmentation rather than claiming perfect VAD.
                result["internal_endpoints"] = [segment["end"] for segment in result["segments"][:-1]]
                result["injected_gap_endpoints"] = [
                    segment["end"] for segment in result["segments"][:-1]
                    if any(gap["start"] <= segment["end"] <= gap["end"] + 0.2 for gap in record["injected_gaps"])
                ]
                rows.append(result)
                checkpoint(args.output, report)
                print(json.dumps({"record": record["id"], "stop_seconds": stop_seconds, "segments": len(segments), "mismatches": sum(part["normalized_parity"] is False for part in result["segments"]), "reference_word_errors": distance}, ensure_ascii=False), flush=True)
        baseline = max(args.stop_seconds)
        baselines = {row["id"]: row for row in rows if row["stop_seconds"] == baseline}
        gates = {}
        for stop_seconds in args.stop_seconds:
            selected = [row for row in rows if row["stop_seconds"] == stop_seconds]
            early_pairs = [segment for row in selected for segment in row["segments"] if segment["early"]]
            additional_human = [row["id"] for row in selected if row["kind"] in ("human", "ordinary") and len(row["internal_endpoints"]) > len(baselines[row["id"]]["internal_endpoints"])]
            injected = [row["id"] for row in selected if row["injected_gap_endpoints"]]
            failed = [row["id"] for row in selected if not row["segments"] or any(not segment["final"]["text"] or segment["final"]["failure"] or (segment["early"] and segment["early"]["failure"]) for segment in row["segments"])]
            gates[str(stop_seconds)] = {
                "records": len(selected), "early_final_pairs": len(early_pairs),
                "normalized_mismatches": sum(not pair["normalized_parity"] for pair in early_pairs),
                "last_token_mismatches": sum(not pair["last_three_tokens_parity"] for pair in early_pairs),
                "digit_mismatches": sum(not pair["digit_parity"] for pair in early_pairs),
                "additional_human_endpoints_vs_baseline": additional_human,
                "critical_injected_pause_endpoints": injected,
                "critical_digit_reference_failures": [row["id"] for row in selected if row["reference_digit_match"] is False],
                "failed_records": failed,
                "enable_preemptive_stt": bool(early_pairs) and not failed and all(pair["normalized_parity"] and pair["digit_parity"] for pair in early_pairs),
                "ordinary_endpoint_pass": not failed and not additional_human,
                "sensitive_endpoint_pass": not failed and not injected,
            }
        ordinary_safe = [value for value in args.stop_seconds if gates[str(value)]["ordinary_endpoint_pass"]]
        sensitive_safe = [value for value in args.stop_seconds if gates[str(value)]["sensitive_endpoint_pass"]]
        ordinary = min(ordinary_safe) if ordinary_safe else baseline
        sensitive = min(value for value in sensitive_safe if value >= ordinary) if any(value >= ordinary for value in sensitive_safe) else baseline
        report["gates"] = gates
        report["recommendation"] = {
            "vad_stop_seconds": ordinary,
            "vad_sensitive_stop_seconds": sensitive,
            "stt_preemptive_enabled": gates[str(ordinary)]["enable_preemptive_stt"] and gates[str(sensitive)]["enable_preemptive_stt"],
            "reason": "Require exact normalized early/final text and digit parity; retain longer sensitive pauses when mid-field recordings split",
            "limits": "Small offline diagnostic corpus; reference WER kept separately. The simulator assumes early inference finishes before the next pause, maximizing exposure to early reuse. Live browser timing must be measured separately.",
        }
        report["unique_decodes"] = len(decode_cache)
        checkpoint(args.output, report)
        print(json.dumps(report["recommendation"], ensure_ascii=False), flush=True)
    finally:
        model.close()


async def inspect_vad(args, inputs):
    rows = []
    for record in inputs["records"]:
        signal = read_audio(record["audio"])
        for stop_seconds in args.stop_seconds:
            segments = await endpoint_segments(signal, stop_seconds, args.early_seconds)
            rows.append({"id": record["id"], "kind": record["kind"], "stop_seconds": stop_seconds, "segments": len(segments), "early_snapshots": sum(segment.early is not None for segment in segments), "endpoints": [round(segment.end, 3) for segment in segments], "injected_gaps": record["injected_gaps"]})
        print(record["id"], "VAD inspected", flush=True)
    checkpoint(args.output, {"scope": "CPU-only VAD inspection; no ASR inference, no quality recommendation", "inputs_manifest": str(args.input_dir / "manifest.json"), "results": rows, "potential_early_final_decodes": sum(row["segments"] + row["early_snapshots"] for row in rows)})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path.home() / ".cache/voice-desk/evaluation/fleurs-ru/dev.json")
    parser.add_argument("--human-limit", type=int, default=15)
    parser.add_argument("--pauses", nargs="+", type=float, default=[0.35, 0.45, 0.6])
    parser.add_argument("--stop-seconds", nargs="+", type=float, default=[0.45, 0.65, 0.95])
    parser.add_argument("--early-seconds", type=float, default=settings.stt_preemptive_silence_seconds)
    parser.add_argument("--input-dir", type=Path, default=ROOT / "artifacts/stt-endpoint-inputs")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/stt-endpoint-validation.json")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--inspect-vad-only", action="store_true")
    parser.add_argument("--reuse-inputs", action="store_true")
    args = parser.parse_args()
    if not args.stop_seconds or any(not 0.15 < value <= 2.0 for value in args.stop_seconds):
        parser.error("VAD stop thresholds must be between 0.15 and 2 seconds")
    if not 0 < args.early_seconds < min(args.stop_seconds):
        parser.error("Early silence must be shorter than every VAD endpoint threshold")
    inputs = json.loads((args.input_dir / "manifest.json").read_text()) if args.reuse_inputs else prepare(args)
    if len({record["id"] for record in inputs["records"]}) != len(inputs["records"]):
        parser.error("Input IDs must be unique; prepare the fixtures again")
    print(f"Prepared {len(inputs['records'])} real/synthetic audio inputs", flush=True)
    if args.inspect_vad_only:
        await inspect_vad(args, inputs)
    elif not args.prepare_only:
        await evaluate(args, inputs)


if __name__ == "__main__":
    asyncio.run(main())
