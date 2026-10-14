"""Isolated sequential GPU ASR comparison on immutable human audio, with raw failures retained."""

import argparse
import importlib.metadata
import json
import multiprocessing
import os
import platform
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf
from asr_metrics import aggregate, alignment, literal_numbers
from download_stt_candidates import EXTERNAL, external_directory, sha256

ROOT = Path(__file__).resolve().parents[1]
ALIASES = ("whisper-turbo-q5", "qwen-asr-0.6b", "qwen-asr-1.7b", "gigaam-v3-rnnt", "whisper-large-v3")
NATIVE_PARAMETERS = {
    "language": "ru",
    "threads": 4,
    "no_context": True,
    "single_segment": True,
    "temperature": 0,
    "beam_size": 3,
    "patience": 1,
    "max_tokens": 160,
    "full_encoder_context": True,
    "flash_attention": True,
    "initial_prompt": "Здравствуйте. Консультация, диагностика, настройка. Беларусь, Минск. Телефон плюс триста семьдесят пять.",
}
QWEN_PARAMETERS = {
    "language": "Russian",
    "max_tokens": 512,
    "temperature": 0,
    "batch_size": 1,
    "chunk_duration": 30,
    "system_prompt": None,
    "hotwords": None,
    "verbose": False,
}


def source_hashes():
    paths = [
        Path(__file__),
        ROOT / "scripts/asr_metrics.py",
        ROOT / "app/stt_native.py",
        ROOT / "native/whisper_bridge.cpp",
        ROOT / ".runtime/libvoice_whisper.dylib",
    ]
    site = Path(sys.prefix) / "lib/python3.12/site-packages"
    paths += [
        site / "mlx_audio/stt/models/qwen3_asr/qwen3_asr.py",
        site / "gigaam_mlx/__init__.py",
        site / "gigaam_mlx/model.py",
        site / "gigaam_mlx/audio.py",
    ]
    return {str(path): sha256(path) for path in paths}


def model_assets(alias):
    if alias == "whisper-turbo-q5":
        path = EXTERNAL / "runtime-models/whisper-turbo-metal.bin"
        expected = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
        if sha256(path) != expected:
            raise ValueError("Baseline pinned model hash mismatch")
        return path, [
            {
                "path": str(path),
                "sha256": expected,
                "bytes": path.stat().st_size,
                "repo": "ggerganov/whisper.cpp",
                "revision": "5359861c739e955e79d9a303bcbc70fb988958b1",
                "file": "ggml-large-v3-turbo-q5_0.bin",
                "license": "MIT",
                "model": alias,
            }
        ]
    records = [
        row
        for row in json.loads((EXTERNAL / "stt-models/manifest.json").read_text())
        if row["model"] == alias
    ]
    if not records or not any(row["file"].endswith((".bin", ".safetensors")) for row in records):
        raise FileNotFoundError("Pinned candidate download is incomplete")
    for record in records:
        path = Path(record["path"]).resolve(strict=True)
        if not path.is_relative_to(EXTERNAL) or sha256(path) != record["sha256"]:
            raise ValueError("Pinned candidate asset mismatch")
    path = EXTERNAL / "stt-models" / alias
    return (path / "ggml-large-v3.bin" if alias == "whisper-large-v3" else path), records


def worker(connection, alias, model_path):
    """The parent can terminate precisely this process if native inference stalls."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(EXTERNAL / "hf-cache")
    # iCloud may retain dataless repository __pycache__ files. Never hydrate those
    # during inference; cache bytecode outside the document provider instead.
    sys.pycache_prefix = str(external_directory(EXTERNAL / "python-bytecode-cache"))
    sys.path.insert(0, str(ROOT))
    started = time.perf_counter()
    model = None
    try:
        if alias.startswith("whisper"):
            from app.stt_native import MetalWhisper

            model = MetalWhisper(model_path, use_gpu=True, compact_context=False)
            metadata = {
                "backend": "whisper.cpp Metal",
                "version": model.version,
                "parameters": NATIVE_PARAMETERS,
            }

            def transcribe(audio):
                return model.transcribe(audio), {}

        elif alias.startswith("qwen"):
            import mlx.core as mx
            from mlx_audio.stt.utils import load_model

            model = load_model(Path(model_path), strict=True)
            metadata = {"backend": "mlx-audio", "parameters": QWEN_PARAMETERS}

            def transcribe(audio):
                result = model.generate(audio, **QWEN_PARAMETERS)
                mx.synchronize()
                return result.text.strip(), {
                    "generation_tokens": result.generation_tokens,
                    "prompt_tokens": result.prompt_tokens,
                    "budget_exhausted": result.generation_tokens >= QWEN_PARAMETERS["max_tokens"],
                }

        else:
            import gigaam_mlx
            import mlx.core as mx

            model, tokenizer = gigaam_mlx.load_model("rnnt", repo_id=model_path)
            metadata = {
                "backend": "gigaam-mlx",
                "parameters": {"model_type": "rnnt", "sample_rate": 16000},
                "annotation": "Pinned repository config incorrectly says CTC; explicit RNNT construction and RNNT decoder/joint tensor keys used.",
            }

            def transcribe(audio):
                mel = gigaam_mlx.compute_mel(audio)
                encoded, seq_len = model.encode(mx.array(mel[np.newaxis]))
                mx.eval(encoded)
                token_ids = model.decode(encoded, seq_len)
                text = tokenizer.decode(token_ids)
                mx.synchronize()
                return text.strip(), {"output_tokens": len(token_ids)}

        connection.send({"ready": True, "loading_s": time.perf_counter() - started, "metadata": metadata})
        while True:
            audio = connection.recv()
            if audio is None:
                break
            start = time.perf_counter()
            try:
                text, details = transcribe(audio)
                result = {"hypothesis": text, "details": details, "error": None}
            except Exception as error:
                result = {"hypothesis": "", "details": {}, "error": type(error).__name__}
            result["elapsed_s"] = time.perf_counter() - start
            result["process_peak_rss_gib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3
            connection.send(result)
    except Exception as error:
        connection.send({"ready": False, "error": type(error).__name__})
    finally:
        if model is not None and hasattr(model, "close"):
            model.close()
        connection.close()


def stop_worker(process, connection):
    if process.is_alive():
        try:
            connection.send(None)
        except (BrokenPipeError, EOFError, OSError):
            pass
        process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    connection.close()


def boot_worker(alias, path, load_timeout):
    parent, child = multiprocessing.get_context("spawn").Pipe()
    process = multiprocessing.get_context("spawn").Process(target=worker, args=(child, alias, str(path)))
    process.start()
    child.close()
    if not parent.poll(load_timeout):
        stop_worker(process, parent)
        raise TimeoutError("ASR model loading exceeded deadline")
    ready = parent.recv()
    if not ready.get("ready"):
        stop_worker(process, parent)
        raise RuntimeError(f"ASR model initialization failed: {ready.get('error')}")
    return process, parent, ready


def metrics(rows):
    if not rows:
        return {}
    elapsed = [row["elapsed_s"] for row in rows]
    result = {
        "normalized": aggregate(rows),
        "raw": aggregate(rows, "raw"),
        "errors": sum(bool(r["error"]) for r in rows),
        "empty_outputs": sum(not r["hypothesis"] for r in rows),
        "base_recordings": len({r["base_id"] for r in rows}),
        "duration_s": sum(r["duration_s"] for r in rows),
        "elapsed_s": sum(elapsed),
        "latency_p50_s": float(np.quantile(elapsed, 0.5)),
        "latency_p95_s": float(np.quantile(elapsed, 0.95)),
        "rtf": sum(elapsed) / sum(r["duration_s"] for r in rows),
        "literal_numbers": aggregate(rows, "literal_numbers"),
    }
    numbered = [r for r in rows if r["has_literal_numbers"]]
    result["numbered_utterances"] = len(numbered)
    result["literal_number_exact_utterances"] = sum(r["literal_numbers"]["exact_sequence"] for r in numbered)
    groups = defaultdict(lambda: [0, 0])
    for row in rows:
        groups[row["base_id"]][0] += row["normalized"]["errors"]
        groups[row["base_id"]][1] += row["normalized"]["reference_words"]
    values = np.array(list(groups.values()))
    rng = np.random.default_rng(20261013)
    boot = values[rng.integers(0, len(values), (1000, len(values)))].sum(axis=1)
    result["wer_cluster_bootstrap_95_interval"] = np.quantile(
        boot[:, 0] / boot[:, 1], [0.025, 0.975]
    ).tolist()
    result["interval_cluster"] = "base recording; speaker IDs unavailable"
    return result


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[f"{row['channel']}/{row['condition']}"].append(row)
        if row["condition"] == "complex":
            groups["complex_all_channels"].append(row)
            groups[f"{row['channel']}/complex/{row['noise']}"].append(row)
            groups[f"{row['channel']}/complex/gender_{row['gender']}"].append(row)
            groups[
                f"{row['channel']}/complex/{'numbers' if row['has_literal_numbers'] else 'no_literal_numbers'}"
            ].append(row)
    return {key: metrics(value) for key, value in groups.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=ALIASES, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=EXTERNAL / "corpora/fleurs-ru-v3/frozen-manifest-v3.json"
    )
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit-bases", type=int)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--load-timeout", type=float, default=180)
    args = parser.parse_args()
    output = external_directory(args.output)
    report_path = output / f"{args.model}-{args.split}.json"
    if report_path.exists():
        raise FileExistsError("Existing frozen result retained; choose a new output directory")
    manifest_path = args.manifest.resolve(strict=True)
    if not manifest_path.is_relative_to(EXTERNAL):
        raise ValueError("Evaluation corpus must remain on external disk")
    expected_manifest = manifest_path.with_suffix(".sha256").read_text().strip()
    if sha256(manifest_path) != expected_manifest:
        raise ValueError("Frozen manifest hash mismatch")
    corpus = json.loads(manifest_path.read_text())
    records = [r for r in corpus["records"] if r["split"] == args.split]
    if args.limit_bases:
        ids = list(dict.fromkeys(r["base_id"] for r in records))[: args.limit_bases]
        records = [r for r in records if r["base_id"] in ids]
    for record in records:
        path = Path(record["audio"]).resolve(strict=True)
        if not path.is_relative_to(EXTERNAL) or sha256(path) != record["sha256"]:
            raise ValueError("Frozen audio hash mismatch")
    model_path, assets = model_assets(args.model)
    versions = {
        name: importlib.metadata.version(name)
        for name in ["mlx", "mlx-audio", "gigaam-mlx", "numpy", "soxr", "scipy", "soundfile"]
    }
    report = {
        "model": args.model,
        "split": args.split,
        "manifest_path": str(manifest_path),
        "manifest_sha256": expected_manifest,
        "source_hashes": source_hashes(),
        "weights": assets,
        "versions": versions,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "timeout_s": args.timeout,
        "requested_records": len(records),
        "scope": corpus["protocol"]["limitations"],
        "complete": False,
        "results": [],
    }
    process = connection = None
    try:
        process, connection, ready = boot_worker(args.model, model_path, args.load_timeout)
        report["model_runtime"] = ready
        warmup_record = next(row for row in corpus["records"] if row["split"] == "dev")
        if sha256(Path(warmup_record["audio"])) != warmup_record["sha256"]:
            raise ValueError("Frozen warmup audio hash mismatch")
        warmup, _ = sf.read(warmup_record["audio"], dtype="float32")
        connection.send(warmup)
        if not connection.poll(args.timeout):
            raise TimeoutError("Unscored fixed dev warmup exceeded deadline")
        report["warmup"] = connection.recv()
        report["warmup"]["id"] = warmup_record["id"]
        for index, record in enumerate(records):
            if not process.is_alive():
                raise RuntimeError("ASR worker exited before dataset completion")
            audio, rate = sf.read(record["audio"], dtype="float32")
            if rate != 16000 or audio.ndim != 1:
                raise ValueError("Prepared audio format changed")
            connection.send(audio)
            if connection.poll(args.timeout):
                decoded = connection.recv()
            else:
                stop_worker(process, connection)
                process = connection = None
                decoded = {
                    "hypothesis": "",
                    "elapsed_s": args.timeout,
                    "error": "InferenceTimeout",
                    "details": {},
                }
                report["results"].append(
                    {
                        **record,
                        **decoded,
                        "normalized": alignment(record["reference"], ""),
                        "raw": alignment(record["reference"], "", normalized=False),
                        "literal_numbers": literal_numbers(record["reference"], ""),
                    }
                )
                report["summary"] = summarize(report["results"])
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                raise TimeoutError(
                    "ASR inference timed out; partial report retains deletions, complete=False"
                )
            hypothesis = decoded["hypothesis"]
            row = {
                **record,
                **decoded,
                "normalized": alignment(record["reference"], hypothesis),
                "raw": alignment(record["reference"], hypothesis, normalized=False),
                "literal_numbers": literal_numbers(record["reference"], hypothesis),
            }
            report["results"].append(row)
            if (index + 1) % 20 == 0 or index + 1 == len(records):
                report["summary"] = summarize(report["results"])
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(
                    f"{args.model} {args.split}: {index + 1}/{len(records)}, {sum(r['elapsed_s'] for r in report['results']):.1f}s inference",
                    flush=True,
                )
        report["complete"] = True
        report["summary"] = summarize(report["results"])
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    key: value["normalized"]
                    for key, value in report["summary"].items()
                    if key.endswith("/complex")
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        if process is not None:
            stop_worker(process, connection)


if __name__ == "__main__":
    main()
