"""Reproducible, synthetic-only native LLM quality and latency comparison.

Runs each weight sequentially on a dedicated loopback llama-server. No database,
contact data, or application actions are used. Output text is diagnostic evidence,
not a certified language-quality score. --source permits a frozen models.py to
keep the comparison stable while application code changes.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402 - standalone script path bootstrap
from app.grounding import fact_sources  # noqa: E402 - standalone script path bootstrap

MODELS = {
    "qwen3-4b": "models/qwen3-4b-q4.gguf",
    "qwen3.5-2b": "models/qwen3.5-2b-q4.gguf",
    "qwen3-1.7b": "models/qwen3-1.7b-q4.gguf",
    "gemma4-e2b": "models/gemma4-e2b-q4.gguf",
    "lfm2.5-2.6b": "models/lfm2.5-2.6b-q4.gguf",
    "smollm3-3b": "models/smollm3-3b-q4.gguf",
    "ministral3-3b": "models/ministral3-3b-q4.gguf",
    "phi4-mini": "models/phi4-mini-q4.gguf",
}
DOWNLOAD_SPECS = {
    "qwen3.5-2b": (
        "unsloth/Qwen3.5-2B-GGUF", "f6d5376be1edb4d416d56da11e5397a961aca8ae",
        "Qwen3.5-2B-Q4_K_M.gguf", "qwen3.5-2b-q4.gguf", "Apache-2.0",
    ),
    "qwen3-1.7b": (
        "unsloth/Qwen3-1.7B-GGUF", "d7f544eead698dbd1f15126ef60b45a1e1933222",
        "Qwen3-1.7B-Q4_K_M.gguf", "qwen3-1.7b-q4.gguf", "Apache-2.0",
    ),
    "gemma4-e2b": (
        "ggml-org/gemma-4-E2B-it-GGUF", "b4243c156154b6dca9324415f8c7ccc098b4aed1",
        "gemma-4-E2B-it-Q4_0.gguf", "gemma4-e2b-q4.gguf", "Apache-2.0",
    ),
    "lfm2.5-2.6b": (
        "LiquidAI/LFM2.5-2.6B-GGUF", "e7caca5d835a3901a8e0d63e94009429bafafdfc",
        "LFM2.5-2.6B-Q4_K_M.gguf", "lfm2.5-2.6b-q4.gguf", "other (LFM)",
    ),
    "smollm3-3b": (
        "ggml-org/SmolLM3-3B-GGUF", "4965cb60b150737b68a0408c36aeefb65078f894",
        "SmolLM3-Q4_K_M.gguf", "smollm3-3b-q4.gguf", "Apache-2.0",
    ),
    "ministral3-3b": (
        "mistralai/Ministral-3-3B-Instruct-2512-GGUF", "eb599d408350ea2bb60452cb86be7c7b2fc28227",
        "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf", "ministral3-3b-q4.gguf", "Apache-2.0",
    ),
    "phi4-mini": (
        "unsloth/Phi-4-mini-instruct-GGUF", "78eb92a46fc37e6b524df991ed9aca9bc6aa7b80",
        "Phi-4-mini-instruct-Q4_K_M.gguf", "phi4-mini-q4.gguf", "MIT (upstream Microsoft)",
    ),
}


# Public immutable weight identity, distinct from the application source freeze.
# Quantization is recorded because effective parameter count alone cannot make
# files of different architectures and quantizers directly interchangeable.
PINNED_WEIGHTS = {
    "gemma4-e2b": {"bytes": 2841481184, "sha256": "8e30dff3ac4c8434c49a7036fa15564bdbb6044e42bf04550bf1a096ad7e6a52", "quantization": "Q4_0"},
    "lfm2.5-2.6b": {"thinking_caveat": "Primary model card specifies always reasoning; requested off flags are not evidence of non-thinking.", "bytes": 1674455040, "sha256": "02a8b7e17487d326e46d68ce0ba24211e1b80a14c4cd0597fa73c1cd697f52ed", "quantization": "Q4_K_M"},
    "smollm3-3b": {"bytes": 1915305312, "sha256": "8334b850b7bd46238c16b0c550df2138f0889bf433809008cc17a8b05761863e", "quantization": "Q4_K_M"},
    "ministral3-3b": {"bytes": 2147023008, "sha256": "9ed150d4367e68df0ac8e1540f6ddc65b42d0ee26378329d1ecbca60f93fc5f8", "quantization": "Q4_K_M"},
    "phi4-mini": {"bytes": 2491874272, "sha256": "88c00229914083cd112853aab84ed51b87bdf6b9ce42f532d8c85c7c63b1730a", "quantization": "Q4_K_M"},
}


def model_path(alias, model_dir=None):
    """The baseline stays local; test weights require explicitly chosen storage."""
    if alias == "qwen3-4b":
        return ROOT / MODELS[alias]
    from scripts.download_models import download_destination

    selected = model_dir or os.environ.get("VOICE_TEST_MODEL_DIR")
    if not selected:
        raise ValueError("Set --model-dir or VOICE_TEST_MODEL_DIR for test weights")
    directory = download_destination(selected)
    target = directory / Path(MODELS[alias]).name
    if not target.resolve().is_relative_to(directory):
        raise ValueError("Test model symlink escapes the selected directory")
    return target


def percentile(values, proportion):
    """Nearest-rank percentile, explicitly retaining the observed tail."""
    return sorted(values)[max(0, math.ceil(len(values) * proportion) - 1)] if values else None


def normalized(field, value):
    if field == "phone" and value:
        return re.sub(r"[\s()\-]", "", value)
    return value


def evaluate(case, output, sources):
    failures = []
    for field, expected in case.get("expected", {}).items():
        allowed = expected if isinstance(expected, list) and field != "fact_quotes" else [expected]
        if normalized(field, output.get(field)) not in [normalized(field, value) for value in allowed]:
            failures.append(f"{field}: expected {expected!r}, observed {output.get(field)!r}")
    for field in case.get("absent", []):
        if output.get(field):
            failures.append(f"unexpected {field}")
    quotes = output.get("fact_quotes", [])
    if any(not q or not any(q in source for source in sources) for q in quotes):
        failures.append("quote is not a contiguous source excerpt")
    if case.get("quote_contains"):
        joined = " ".join(quotes).lower()
        if not all(term.lower() in joined for term in case["quote_contains"]):
            failures.append("quotes omit the requested published fact")
    answer = output.get("answer", "")
    if case.get("answer_contains") and not any(re.search(pattern, answer, re.I) for pattern in case["answer_contains"]):
        failures.append("answer does not address the expected topic/history")
    if case.get("answer_required") and len(answer.strip()) < 30:
        failures.append("answer too short to address conversation")
    if re.search(r"(?:записал[аи]?|соединил[аи]?|перев[её]л[аи]?|позвонил[аи]?)\s+(?:вас|вам|сотруднику)|(?:запись|оплата|заявка)\s+(?:подтверждена|выполнена|сохранена)", answer.lower()):
        failures.append("claims an action already happened")
    if re.search(r"<think>|</think>", answer):
        failures.append("thinking leaked into answer")
    return failures


def components(case, output, failures):
    """Separate strict omission gates from semantic intent/field correctness."""
    checks = {"schema": output is not None}
    if output is None:
        # An unusable response cannot silently disappear from a quality gate's
        # denominator. Keep each criterion's fixture applicability unchanged.
        checks.update({f"field:{field}": False for field in case.get("expected", {})})
        if case.get("absent"):
            checks["omit_unmentioned_fields"] = False
        if case.get("quote_contains") or "fact_quotes" in case.get("expected", {}):
            checks["grounding"] = False
        if case.get("answer_required") or case.get("answer_contains"):
            checks["conversation_topic"] = False
        checks["no_action_claim"] = False
        return checks
    expected = case.get("expected", {})
    for field, desired in expected.items():
        allowed = desired if isinstance(desired, list) and field != "fact_quotes" else [desired]
        checks[f"field:{field}"] = normalized(field, output.get(field)) in [normalized(field, value) for value in allowed]
    if case.get("absent"):
        checks["omit_unmentioned_fields"] = not any(output.get(field) for field in case["absent"])
    if case.get("quote_contains") or "fact_quotes" in expected:
        checks["grounding"] = not any("quote" in failure for failure in failures)
    if case.get("answer_required") or case.get("answer_contains"):
        checks["conversation_topic"] = not any("answer" in failure for failure in failures)
    checks["no_action_claim"] = "claims an action already happened" not in failures
    return checks


def summary(rows):
    successes = [row for row in rows if "output" in row]
    times = [row["elapsed_seconds"] for row in rows]
    speed = [row["timings"]["predicted_per_second"] for row in successes if row.get("timings", {}).get("predicted_per_second")]
    groups = sorted({row["group"] for row in rows})
    check_names = sorted({check for row in rows for check in row.get("checks", {})})
    return {
        "cases": len(rows),
        "valid_schema": len(successes),
        "passed": sum(not row["failures"] for row in rows),
        "elapsed_p50_seconds": percentile(times, .5),
        "elapsed_p95_seconds": percentile(times, .95),
        "generation_tps_median": statistics.median(speed) if speed else None,
        "components": {check: {"cases": sum(check in row.get("checks", {}) for row in rows), "passed": sum(bool(row.get("checks", {}).get(check)) for row in rows)} for check in check_names},
        "by_group": {group: {"cases": sum(row["group"] == group for row in rows), "passed": sum(row["group"] == group and not row["failures"] for row in rows)} for group in groups},
    }


async def wait_ready(process, base):
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError("llama-server exited before readiness; inspect its dedicated log")
            try:
                response = await client.get(base + "/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.5)
    raise RuntimeError("llama-server readiness timeout")


async def stream_probe(client, url, payload):
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    start = time.perf_counter()
    first = None
    sentence = None
    content = ""
    usage = None
    async with client.stream("POST", url, json=payload, timeout=40) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            data = json.loads(line[6:])
            usage = data.get("usage") or usage
            choices = data.get("choices", [])
            chunk = choices[0].get("delta", {}).get("content", "") if choices else ""
            if chunk:
                first = first if first is not None else time.perf_counter() - start
                content += chunk
                answer_start = re.search(r'"answer"\s*:\s*"', content)
                if answer_start and sentence is None and re.search(r'[.!?](?:\s|"|$)', content[answer_start.end():]):
                    sentence = time.perf_counter() - start
    return {"ttft_seconds": first, "first_answer_sentence_seconds": sentence, "elapsed_seconds": time.perf_counter() - start, "usage": usage, "content": content}


async def run_model(alias, args, suite, models_class):
    path = model_path(alias, args.model_dir)
    if not path.exists():
        raise FileNotFoundError(path)
    manifests = [ROOT / "model-manifest.json", args.output / "candidate-model-manifest.json", ROOT / "artifacts/latency-20261013/candidate-model-manifest.json"]
    expected = next((entry for manifest in manifests if manifest.exists() for entry in json.loads(manifest.read_text()) if entry["file"] == path.name), None)
    if not expected:
        raise RuntimeError(f"No verified manifest for {path.name}; run --download first")
    with path.open("rb") as model_file:
        sha256 = hashlib.file_digest(model_file, "sha256").hexdigest()
    if sha256 != expected["sha256"]:
        raise RuntimeError(f"SHA256 mismatch: {path.name}")
    pinned = PINNED_WEIGHTS.get(alias, {})
    if pinned and (sha256 != pinned["sha256"] or path.stat().st_size != pinned["bytes"]):
        raise RuntimeError(f"Pinned weight identity mismatch: {path.name}")
    evidence = dict(expected, path=str(path.resolve()), quantization=pinned.get("quantization", "Q4_K_M"), thinking_caveat=pinned.get("thinking_caveat"))
    (args.output / f"{alias}-weights.json").write_text(json.dumps(evidence, indent=2) + "\n")
    base = f"http://127.0.0.1:{args.port}"
    # Refuse to replace a listener, including another concurrent experiment.
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        try:
            await client.get(base + "/health")
        except httpx.ConnectError:
            pass
        else:
            raise RuntimeError(f"Benchmark port {args.port} is in use")
    command = [shutil.which("llama-server"), "-m", str(path), "--host", "127.0.0.1", "--port", str(args.port), "-c", str(args.context), "-ngl", "99", "-t", "4", "--parallel", "1", "--alias", alias, "--jinja", "--reasoning", "off", "--chat-template-kwargs", '{"enable_thinking":false}', "--log-disable"]
    if args.reasoning_budget is not None:
        command += ["--reasoning-budget", str(args.reasoning_budget)]
    print(f"Starting {alias}", flush=True)
    records = []
    probes = []
    with (args.output / f"{alias}-server.log").open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            await wait_ready(process, base)
            settings.llm_url = base + "/v1"
            settings.llm_model = alias
            settings.llm_api_key = ""
            settings.llm_context_tokens = args.context
            settings.llm_reply_tokens = args.reply_tokens
            models = models_class()
            captured = {}

            async def request_hook(request):
                if request.url.path.endswith("/chat/completions"):
                    captured["payload"] = json.loads(request.content)

            async def response_hook(response):
                if response.request.url.path.endswith("/chat/completions"):
                    await response.aread()
                    if response.status_code == 200:
                        captured["response"] = response.json()

            models.http.event_hooks = {"request": [request_hook], "response": [response_hook]}
            try:
                # A reasoning-only or incompatible model may fail warmup. Retain
                # that evidence and still measure the requested compatibility cases.
                warmup = {}
                try:
                    await models.extract("Здравствуйте, как вы можете помочь?", [], suite["context"], suite["services"], suite["knowledge"], suite["now"])
                    warmup["valid_schema"] = True
                except Exception as exc:
                    warmup.update(valid_schema=False, error_type=type(exc).__name__)
                warm_response = captured.get("response", {})
                warm_message = warm_response.get("choices", [{}])[0].get("message", {})
                warmup.update(usage=warm_response.get("usage"), finish_reason=warm_response.get("choices", [{}])[0].get("finish_reason"), content_characters=len(warm_message.get("content") or ""), reasoning_characters=len(warm_message.get("reasoning_content") or warm_message.get("reasoning") or ""))
                (args.output / f"{alias}-warmup.json").write_text(json.dumps(warmup, ensure_ascii=False, indent=2) + "\n")
                for case in suite["cases"]:
                    captured.clear()
                    start = time.perf_counter()
                    row = {"id": case["id"], "group": case["group"]}
                    try:
                        result = await models.extract(case["text"], case.get("history", []), dict(suite["context"], **case.get("context", {})), suite["services"], suite["knowledge"], suite["now"])
                        row["elapsed_seconds"] = time.perf_counter() - start
                        row["output"] = result.model_dump()
                        row["failures"] = evaluate(case, row["output"], fact_sources(suite["services"], suite["knowledge"]))
                        response = captured.get("response", {})
                        row["usage"] = response.get("usage")
                        row["timings"] = response.get("timings", {})
                        row["finish_reason"] = response.get("choices", [{}])[0].get("finish_reason")
                        message = response.get("choices", [{}])[0].get("message", {})
                        row["reasoning_characters"] = len(message.get("reasoning_content") or message.get("reasoning") or "")
                    except Exception as exc:
                        row.update(elapsed_seconds=time.perf_counter() - start, error_type=type(exc).__name__, failures=["request or schema validation failed"])
                        response = captured.get("response", {})
                        row["usage"] = response.get("usage")
                        row["timings"] = response.get("timings", {})
                        row["finish_reason"] = response.get("choices", [{}])[0].get("finish_reason")
                        # Synthetic-only diagnostic evidence distinguishes truncation
                        # from transport failures without recording error secrets.
                        row["raw_content"] = response.get("choices", [{}])[0].get("message", {}).get("content")
                        message = response.get("choices", [{}])[0].get("message", {})
                        row["reasoning_characters"] = len(message.get("reasoning_content") or message.get("reasoning") or "")
                    row["checks"] = components(case, row.get("output"), row["failures"])
                    records.append(row)
                    print(f"{alias} {case['id']}: {row['elapsed_seconds']:.2f}s {'PASS' if not row['failures'] else 'FAIL ' + '; '.join(row['failures'])}", flush=True)
                    (args.output / f"{alias}-extract.json").write_text(json.dumps({"summary": summary(records), "records": records}, ensure_ascii=False, indent=2) + "\n")
                    if case.get("stream_probe") and captured.get("payload"):
                        async with httpx.AsyncClient(trust_env=False, timeout=40) as stream_client:
                            probe = await stream_probe(stream_client, base + "/v1/chat/completions", captured["payload"])
                        probes.append(dict(id=case["id"], **probe))
                        (args.output / f"{alias}-stream.json").write_text(json.dumps(probes, ensure_ascii=False, indent=2) + "\n")
            finally:
                await models.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    return summary(records)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--model-dir", type=Path, default=os.environ.get("VOICE_TEST_MODEL_DIR"), help="Existing directory for test weights; baseline 4B remains in repository models/")
    parser.add_argument("--suite", type=Path, default=ROOT / "tests/fixtures/llm_receptionist_scenarios.json")
    parser.add_argument("--source", type=Path, default=ROOT / "app/models.py")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/latency-20261013")
    parser.add_argument("--port", type=int, default=18182)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--reply-tokens", type=int, default=384)
    parser.add_argument("--reasoning-budget", type=int, default=None, help="Explicit native thinking cutoff; separate experiment identity from default model behavior")
    parser.add_argument("--profile", choices=("legacy", "compact"), default="legacy")
    parser.add_argument("--download", action="store_true", help="Download only pinned candidates and verify SHA256; preserve the project manifest")
    args = parser.parse_args()
    if any(alias in DOWNLOAD_SPECS for alias in args.models):
        # Validate before creating output directories or touching a downloader.
        first_candidate = next(alias for alias in args.models if alias in DOWNLOAD_SPECS)
        args.model_dir = model_path(first_candidate, args.model_dir).parent
    args.output.mkdir(parents=True, exist_ok=True)
    if args.download:
        from scripts.download_models import download

        manifest_path = args.output / "candidate-model-manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
        for alias in args.models:
            if alias in DOWNLOAD_SPECS:
                item = await asyncio.to_thread(download, *DOWNLOAD_SPECS[alias], destination=args.model_dir)
                manifest = [entry for entry in manifest if entry["file"] != item["file"]] + [item]
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return
    if hasattr(settings, "llm_prompt_profile"):
        settings.llm_prompt_profile = args.profile
    if hasattr(settings, "llm_thinking"):
        settings.llm_thinking = False
    suite = json.loads(args.suite.read_text())
    spec = importlib.util.spec_from_file_location("app._benchmark_models", args.source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = {"platform": platform.platform(), "python": sys.version.split()[0], "llama_version": subprocess.run(["llama-server", "--version"], capture_output=True, text=True).stderr.strip(), "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(), "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(), "context": args.context, "reply_tokens": args.reply_tokens, "prompt_profile": args.profile, "warmup_cases": 1, "stream_probes_are_repeats": True, "parallel": 1, "threads": 4, "gpu_layers": 99, "thinking_requested": False, "reasoning_budget": args.reasoning_budget, "reasoning_character_counts_are_reported_API_fields_only": True, "jinja": True, "limitations": "Synthetic extraction and repeated warm stream probes only. No STT/TTS/audio or end-to-end timing; no human conversation ratings; no production contacts or writes."}
    for name in ("schemas.py", "grounding.py", "fast_intents.py"):
        metadata[name.replace(".py", "_sha256")] = hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest()
    prompt_path = ROOT / "app/llm_prompt.py"
    if args.profile == "compact":
        metadata["compact_prompt_sha256"] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    metadata["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    metadata["model_dir"] = str(args.model_dir) if args.model_dir is not None else None
    support_modules = sorted((ROOT / "app").glob("llm_*.py"))
    metadata["llm_support_sha256"] = {module.name: hashlib.sha256(module.read_bytes()).hexdigest() for module in support_modules}
    metadata_path = args.output / "benchmark-metadata.json"
    if metadata_path.exists():
        previous = json.loads(metadata_path.read_text())
        identity = ("source_sha256", "suite_sha256", "context", "reply_tokens", "prompt_profile", "compact_prompt_sha256", "schemas_sha256", "grounding_sha256", "fast_intents_sha256", "runner_sha256", "llm_support_sha256", "model_dir", "reasoning_budget")
        if any(previous.get(key, "legacy" if key == "prompt_profile" else None) != metadata.get(key) for key in identity):
            raise RuntimeError("Output directory contains a different experiment; select a new --output directory")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    # Uncommitted application changes are part of this experiment. Retain the
    # exact inputs beside the measurements instead of depending on future Git state.
    snapshots = {
        "models-source.py": args.source,
        "scenarios.json": args.suite,
        "schemas-source.py": ROOT / "app/schemas.py",
        "grounding-source.py": ROOT / "app/grounding.py",
        "fast-intents-source.py": ROOT / "app/fast_intents.py",
    }
    if args.profile == "compact":
        snapshots["compact-prompt-source.py"] = prompt_path
    snapshots["benchmark-source.py"] = Path(__file__)
    for support_module in support_modules:
        snapshots[support_module.stem + "-source.py"] = support_module
    for name, source in snapshots.items():
        (args.output / name).write_bytes(source.read_bytes())
    comparison_path = args.output / "comparison.json"
    summaries = json.loads(comparison_path.read_text()) if comparison_path.exists() else {}
    for alias in args.models:
        summaries[alias] = await run_model(alias, args, suite, module.LocalModels)
        (args.output / "comparison.json").write_text(json.dumps(summaries, indent=2) + "\n")
    print(json.dumps(summaries, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
