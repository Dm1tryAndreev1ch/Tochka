#!/usr/bin/env python3
"""Sanitized, read-only runtime baseline. Never imports app.config or reads .env."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = 1
MAX_SOURCE_BYTES = 32 * 1024 * 1024
SOURCE_DIRS = {"app", "scripts", "tests", "native", "launchers"}
SOURCE_EXTENSIONS = {".py", ".js", ".mjs", ".ts", ".tsx", ".css", ".html", ".sql", ".c", ".cpp", ".h", ".sh", ".ps1", ".cmd", ".command"}
ROOT_SOURCE_FILES = {"pyproject.toml", "requirements.lock", "requirements.txt", "requirements-portable.txt",
                     "package.json", "package-lock.json", "docker-compose.yml", "compose.yaml",
                     "Dockerfile", "Makefile", "playwright.config.js", "playwright.ui-isolated.config.js"}
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "__pycache__", "assets", "uploads", "upload", "media",
                  "data", "cache", ".cache", "private", "secrets", "keys", "credentials", "fixtures",
                  "artifacts", "output", "recordings", "backups", "logs", "sessions", ".runtime"}
SENSITIVE_NAME = re.compile(r"(?:^|[._-])(?:secret|secrets|credential|credentials|private|key|keys|token|tokens|password|session|sessions)(?:[._-]|$)", re.I)
ENUM_FIELDS = {
    "stt_backend": {"faster-whisper", "whisper.cpp"},
    "llm_prompt_profile": {"compact", "legacy"},
    "llm_model": {"qwen3-4b", "qwen3-8b", "qwen3-0.6b"},
    "natural_tts_voice": {"ryan", "aiden", "uncle_fu", "serena", "vivian"},
    "natural_tts_male_voice": {"ryan", "aiden", "uncle_fu", "serena", "vivian"},
    "natural_tts_female_voice": {"ryan", "aiden", "uncle_fu", "serena", "vivian"},
}
BOOL_FIELDS = {"llm_remote", "llm_thinking", "natural_tts_enabled", "stt_preemptive_enabled"}
NUMERIC_FIELDS = {
    "llm_threads": (1, 4096), "llm_context_tokens": (1, 4_194_304), "llm_reply_tokens": (1, 1_048_576),
    "vad_stop_seconds": (0, 120), "vad_sensitive_stop_seconds": (0, 120),
    "stt_preemptive_silence_seconds": (0, 120), "tts_streaming_interval": (0, 120),
    "tts_cache_max_bytes": (0, 1024**4), "tts_cache_max_entries": (0, 1_000_000),
    "male_speech_rate": (0.25, 4),
}
PATH_FIELDS = {"llm_model_path", "whisper_model", "whisper_metal_model", "piper_model", "natural_tts_model", "voice_reference_dir"}
ALLOWED_CONFIG = set(ENUM_FIELDS) | BOOL_FIELDS | set(NUMERIC_FIELDS) | PATH_FIELDS | {"llm_gpu_layers"}
PACKAGES = ("pipecat-ai", "faster-whisper", "ctranslate2", "piper-tts", "onnxruntime", "mlx", "mlx-audio",
            "numpy", "soxr", "torch", "transformers", "tokenizers", "httpx", "fastapi", "uvicorn", "pydantic", "cryptography")
TTS_FILES = ("config.json", "generation_config.json", "merges.txt", "model.safetensors",
             "model.safetensors.index.json", "preprocessor_config.json", "speech_tokenizer/config.json",
             "speech_tokenizer/configuration.json", "speech_tokenizer/model.safetensors",
             "speech_tokenizer/preprocessor_config.json", "tokenizer_config.json", "vocab.json")
LAYOUTS = {
    "llm_model_path": {"qwen3-4b-q4.gguf": ("qwen3-4b-q4.gguf",)},
    "whisper_metal_model": {n: (n,) for n in ("whisper-small-metal.bin", "whisper-turbo-metal.bin")},
    "whisper_model": {"whisper-small": tuple("whisper-small/" + n for n in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"))},
    "piper_model": {n: (n, n + ".json") for n in ("ru_RU-denis-medium.onnx", "ru_RU-irina-medium.onnx")},
    "natural_tts_model": {n: tuple(n + "/" + f for f in TTS_FILES) for n in ("qwen3-tts-base-0.6b", "qwen3-tts-0.6b-8bit")},
}
KNOWN_MODEL_FILES = {f for layouts in LAYOUTS.values() for files in layouts.values() for f in files}
UPSTREAM_BASENAMES = {
    "qwen3-4b-q4.gguf": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    "whisper-small-metal.bin": "ggml-small.bin",
    "whisper-turbo-metal.bin": "ggml-large-v3-turbo-q5_0.bin",
}
KNOWN_REPOS = {"unsloth/Qwen3-4B-Instruct-2507-GGUF", "ggerganov/whisper.cpp", "Systran/faster-whisper-small",
               "rhasspy/piper-voices", "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
               "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"}


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def no_symlinks(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            return False
    return True


def file_hash(path: Path, *, maximum=None, git_algorithm="sha1") -> dict:
    before = path.stat()
    size = before.st_size
    if maximum is not None and size > maximum:
        return {"status": "too_large_not_read", "bytes": size}
    sha = hashlib.sha256()
    blob = hashlib.new(git_algorithm)
    blob.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
            blob.update(chunk)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ino) != (size, before.st_mtime_ns, before.st_ino):
        return {"status": "changed_during_read"}
    return {"status": "hashed", "bytes": size, "sha256": sha.hexdigest(), "git_blob": blob.hexdigest()}


def source_allowed(name: str) -> bool:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", name):
        return False
    if any(part.lower() in EXCLUDED_PARTS or part.startswith(".") for part in path.parts):
        return False
    if SENSITIVE_NAME.search(path.name) and name != "app/static/brand/tokens.css":
        return False
    if name in ROOT_SOURCE_FILES or name == "scripts/model-download-catalog.json":
        return True
    if len(path.parts) == 1:
        return path.name.startswith("launch") and path.suffix in SOURCE_EXTENSIONS
    return path.parts[0] in SOURCE_DIRS and path.suffix in SOURCE_EXTENSIONS


def git_output(root, *args):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                                capture_output=True, timeout=15, env=env, check=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def collect_source(root):
    commit_raw = git_output(root, "rev-parse", "--verify", "HEAD")
    commit = commit_raw.decode("ascii", errors="ignore").strip() if commit_raw else ""
    commit = commit if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) else None
    tree = git_output(root, "ls-tree", "-rz", "HEAD") if commit else None
    head = {}
    if tree:
        for item in tree.split(b"\0"):
            try:
                meta, name = item.decode("utf-8").split("\t", 1)
                mode, kind, oid = meta.split()
                if kind == "blob" and source_allowed(name):
                    head[name] = oid
            except (UnicodeDecodeError, ValueError):
                continue
    names = set(head)
    for directory in SOURCE_DIRS:
        start = root / directory
        if not start.is_dir() or start.is_symlink():
            continue
        for base, dirs, files in os.walk(start, followlinks=False):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED_PARTS and not d.startswith(".")
                       and not (Path(base) / d).is_symlink()]
            for filename in files:
                name = (Path(base) / filename).relative_to(root).as_posix()
                if source_allowed(name):
                    names.add(name)
    for path in root.iterdir():
        if source_allowed(path.name):
            names.add(path.name)
    records = []
    for name in sorted(names):
        path = root / name
        record = {"file": name}
        try:
            if not no_symlinks(path, root):
                info = {"status": "symlink_not_read"}
            elif not path.is_file():
                info = {"status": "missing"}
            else:
                info = file_hash(path, maximum=MAX_SOURCE_BYTES,
                                 git_algorithm="sha256" if commit and len(commit) == 64 else "sha1")
            blob = info.pop("git_blob", None)
            change = ("deleted" if info["status"] == "missing" else "modified") if name in head else "added"
            record.update(info, change="unchanged" if blob and blob == head.get(name) else change)
        except OSError:
            record.update(status="unreadable", change="unknown")
        records.append(record)
    changes = [r for r in records if r["change"] != "unchanged"]
    return {"head_commit": commit, "git_available": tree is not None, "dirty_scoped": bool(changes) if tree is not None else None,
            "fingerprints_complete": all(r["status"] in {"hashed", "missing"} for r in records),
            "files": records, "tree_sha256": fingerprint(records), "changes_sha256": fingerprint(changes),
            "coverage": "allowlisted source only; no raw diff, ignored private data, or complete-repository cleanliness claim"}


def literal(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Field":
        node = next((k.value for k in node.keywords if k.arg == "default"), None)
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = literal(node.left), literal(node.right)
        if type(left) in (int, float) and type(right) in (int, float):
            return left * right if isinstance(node.op, ast.Mult) else left + right
    return None


def configuration(root, supplied):
    defaults = {}
    path = root / "app/config.py"
    if no_symlinks(path, root) and path.is_file() and path.stat().st_size <= MAX_SOURCE_BYTES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            settings = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings"), None)
            for node in settings.body if settings else []:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in ALLOWED_CONFIG:
                    value = literal(node.value)
                    if value is not None:
                        defaults[node.target.id] = value
        except (SyntaxError, UnicodeError, OSError):
            pass
    safe, raw_paths, issues = {}, {}, []
    for key in sorted(ALLOWED_CONFIG):
        value = supplied.get(key.upper(), supplied.get(key, defaults.get(key)))
        if value is None:
            issues.append(key + ":unavailable")
            continue
        result = None
        if key in PATH_FIELDS:
            if isinstance(value, (str, Path)) and "\0" not in str(value):
                raw_paths[key] = str(value)
                known = Path(str(value)).name in LAYOUTS.get(key, {})
                if key == "voice_reference_dir":
                    known = str(value).replace("\\", "/").rstrip("/").endswith(("app/assets/voices", "models/voices"))
                result = "known-layout" if known else "custom-path-redacted"
        elif key in ENUM_FIELDS:
            result = value if isinstance(value, str) and value in ENUM_FIELDS[key] else "custom-value-redacted"
        elif key in BOOL_FIELDS:
            if type(value) is bool:
                result = value
            elif isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                result = value.lower() in {"true", "1", "yes", "on"}
        elif key in NUMERIC_FIELDS:
            try:
                number = float(value) if not isinstance(value, bool) else math.nan
                low, high = NUMERIC_FIELDS[key]
                if math.isfinite(number) and low <= number <= high:
                    result = int(number) if number.is_integer() else number
            except (ValueError, TypeError, OverflowError):
                pass
        elif key == "llm_gpu_layers":
            if str(value) in {"auto", "all"}:
                result = str(value)
            elif re.fullmatch(r"\d{1,5}", str(value)):
                result = int(value)
        safe[key] = result if result is not None else "invalid-redacted"
        if result is None or result in ("custom-value-redacted", "custom-path-redacted"):
            issues.append(key + ":redacted_or_invalid")
    return safe, raw_paths, issues


def model_metadata(root):
    records = {}
    # Current catalog takes precedence over historical research/runtime snapshots.
    for relative in ("model-manifest.json", "scripts/model-download-catalog.json"):
        path = root / relative
        if not no_symlinks(path, root) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = data.get("artifacts", []) if isinstance(data, dict) else data
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict) or item.get("file") not in KNOWN_MODEL_FILES:
                    continue
                digest, revision = item.get("sha256", ""), item.get("revision", "")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    continue
                if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
                    continue
                size = item.get("bytes")
                if type(size) is not int or not 0 < size < 1024**5:
                    continue
                records[item["file"]] = {"expected_sha256": digest, "expected_bytes": size,
                                         "revision": revision, "repository": item.get("repo") if item.get("repo") in KNOWN_REPOS else "custom-redacted",
                                         "provenance": relative + ":declared-snapshot"}
        except (ValueError, TypeError, OSError):
            continue
    return records


def configured_path(root, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def verify_model(path, expected):
    # External model symlinks are allowed for read-only fingerprinting, but never
    # follow a model-looking link to .env, a key file, or an unknown filename.
    try:
        resolved = path.resolve()
        denied = EXCLUDED_PARTS - {"assets", "data", "cache", ".cache", ".runtime"}
        unsafe = any(p.lower() in denied for index, p in enumerate(resolved.parts)
                     if not (index == 1 and p == "private" and platform.system() == "Darwin"))
        permitted_names = {path.name, UPSTREAM_BASENAMES.get(path.name)}
        if resolved.name not in permitted_names or unsafe:
            return {"verification": "path_not_approved", "verified": False}
        if not resolved.is_file():
            return {"verification": "missing", "verified": False}
        info = file_hash(resolved)
        info.pop("git_blob", None)
        actual = info.get("sha256")
        good = info.get("status") == "hashed" and actual == expected.get("expected_sha256") and info.get("bytes") == expected.get("expected_bytes")
        return {"verification": "verified" if good else "mismatch", "verified": good,
                "actual_sha256": actual, "actual_bytes": info.get("bytes")}
    except (OSError, ValueError):
        return {"verification": "unreadable", "verified": False}


def collect_models(root, safe_config, raw_paths, verify_models, role="app"):
    metadata = model_metadata(root)
    fields = ["piper_model"] if role == "app" else []
    if role == "app":
        fields.append("whisper_metal_model" if safe_config.get("stt_backend") == "whisper.cpp" else "whisper_model")
    if role == "llm" or (role == "app" and not safe_config.get("llm_remote", False)):
        fields.append("llm_model_path")
    if role == "tts" or (role == "app" and safe_config.get("natural_tts_enabled") is True):
        fields.append("natural_tts_model")
    result = []
    for field in fields:
        value = raw_paths.get(field, "")
        basename = Path(value).name
        files = LAYOUTS[field].get(basename, ())
        if not files:
            result.append({"component": field, "verification": "custom_path_not_inspected", "verified": False})
            continue
        base = configured_path(root, value)
        directory_model = field in {"whisper_model", "natural_tts_model"}
        for filename in files:
            path = base / Path(filename).relative_to(basename) if directory_model else base.parent / filename
            declared = metadata.get(filename, {})
            record = {"component": field, "artifact": filename, **declared,
                      "verification": "not_verified" if declared else "no_pinned_metadata", "verified": False}
            if verify_models and declared:
                record.update(verify_model(path, declared))
            result.append(record)
    if role == "app" and safe_config.get("llm_remote") is True:
        result.append({"component": "llm_model_path", "verification": "remote_runtime_not_observed", "verified": False})
    return result


def collect_voices(root, raw_paths):
    records = []
    for name in ("male.wav", "female.wav", "metadata.json"):
        path = root / "app/assets/voices" / name
        record = {"artifact": "bundled-voices/" + name}
        try:
            if not no_symlinks(path, root):
                record["status"] = "symlink_not_read"
            elif path.is_file():
                info = file_hash(path, maximum=MAX_SOURCE_BYTES)
                info.pop("git_blob", None)
                record.update(info)
            else:
                record["status"] = "missing"
        except OSError:
            record["status"] = "unreadable"
        records.append(record)
    value = raw_paths.get("voice_reference_dir", "")
    selected = "bundled" if value and configured_path(root, value).absolute() == (root / "app/assets/voices").absolute() else "configured_references_not_verified"
    return {"selected_reference_source": selected, "bundled_files": records}


def collect_environment():
    versions = {}
    for package in PACKAGES:
        try:
            value = importlib.metadata.version(package)
            versions[package] = value if re.fullmatch(r"[0-9][0-9A-Za-z.!+_-]{0,63}", value) else "version-redacted"
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    release = re.match(r"[0-9]+(?:\.[0-9]+)*", platform.release())
    machine = platform.machine()
    memory = None
    try:
        pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            memory = pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return {"python": platform.python_version(), "implementation": sys.implementation.name,
            "os": platform.system(), "os_release_numeric": release[0] if release else None,
            "architecture": machine if machine in {"arm64", "aarch64", "x86_64", "AMD64", "x86", "i386", "i686"} else "other",
            "logical_cpu_count": os.cpu_count(), "physical_memory_bytes": memory, "packages": versions,
            "gpu": "not_probed", "native_binary_versions": "not_probed"}


def collect_runtime_manifest(root=ROOT, config: Mapping | None = None, verify_models=False, role="app"):
    """Accept an env/settings mapping; unknown keys and raw paths never enter output.

    Caller should pass the actual launcher's effective mapping. If omitted, only
    process environment + literal source defaults are used, never dotenv files.
    No service/network calls, model imports or writes are performed.
    """
    if role not in {"app", "llm", "tts"}:
        raise ValueError("Manifest role must be app, llm or tts")
    root = Path(root).resolve()
    safe, paths, issues = configuration(root, os.environ if config is None else config)
    semantic = {"schema_version": VERSION, "role": role, "source": collect_source(root), "configuration": safe,
                "configuration_issues": issues, "models": collect_models(root, safe, paths, verify_models, role),
                "voice_references": collect_voices(root, paths), "runtime": collect_environment()}
    return {**semantic, "semantic_sha256": fingerprint(semantic),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "configuration_input": "process-environment-and-source-defaults-no-dotenv" if config is None else "caller-mapping-and-source-defaults",
            "limitations": ["A metadata snapshot is not model-byte verification or an inference benchmark.",
                            "Source coverage excludes private data and is not a full repository or supply-chain attestation.",
                            "Remote worker/GPU/native binary identity requires a separate observed runtime report."]}


def write_runtime_manifest(path, manifest):
    """Create a new JSON file exclusively; never overwrite an existing artifact."""
    path = Path(path)
    if path.suffix.lower() != ".json" or path.is_symlink():
        raise ValueError("Manifest output must be a new .json file, not a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids replacing configs, historical baselines or a
    # concurrent launch's manifest. Use a unique filename for each launch.
    with path.open("x", encoding="utf-8") as stream:
        try:
            json.dump(manifest, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            path.unlink(missing_ok=True)
            raise
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New JSON output; existing files are never overwritten")
    parser.add_argument("--role", choices=("app", "llm", "tts"), default="app", help="Component whose model selection is recorded")
    parser.add_argument("--dry-run", action="store_true", help="Collect and print a compact sanitized summary, without writes")
    parser.add_argument("--verify-models", action="store_true", help="Explicitly read/hash selected model weights, possibly many GB")
    args = parser.parse_args(argv)
    if not args.dry_run and args.output is None:
        parser.error("Pass --output FILE.json or --dry-run")
    try:
        manifest = collect_runtime_manifest(verify_models=args.verify_models, role=args.role)
        if not args.dry_run:
            write_runtime_manifest(args.output, manifest)
        print(json.dumps({"semantic_sha256": manifest["semantic_sha256"],
                          "source_files": len(manifest["source"]["files"]),
                          "dirty_scoped": manifest["source"]["dirty_scoped"],
                          "model_artifacts": len(manifest["models"]),
                          "models_verified": sum(m["verified"] for m in manifest["models"]),
                          "configuration_issues": manifest["configuration_issues"],
                          "dry_run": args.dry_run}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError):
        # Filesystem errors can contain personal absolute paths. Keep CLI errors
        # generic; callers receive the original exception for controlled handling.
        print("Manifest collection/write failed; check source access and use a new writable .json output.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
