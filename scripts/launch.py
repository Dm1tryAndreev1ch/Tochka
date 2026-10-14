"""Cross-platform entry point. Planning and diagnostics never start services."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.runtime_environment import build_child_environment, configured_env_file  # noqa: E402


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--role", choices=("app", "llm"), default="app")
    result.add_argument("--mode", choices=("local", "distributed"), default="local")
    result.add_argument("--speech", choices=("configured", "mac-metal", "portable"), default="configured")
    result.add_argument("--no-telegram", action="store_true", help="Explicitly disable the Telegram worker")
    result.add_argument("--doctor", action="store_true", help="Check files and dependencies; start nothing")
    result.add_argument("--dry-run", action="store_true", help="Print the plan without network or writes")
    result.add_argument("--download-models", action="store_true", help="Provision models before startup")
    result.add_argument("--download-only", action="store_true", help="Provision models and exit without startup")
    result.add_argument("--model-dir", type=Path, help="Use the provisioner's standard model paths here")
    result.add_argument("--llm-url", help="OpenAI-compatible URL ending in /v1")
    result.add_argument("--llm-api-key-file", type=Path, help="Read the token from this file; never print it")
    result.add_argument("--model-path", type=Path, help="GGUF file (worker or local app)")
    result.add_argument("--model", help="Model alias; default qwen3-4b")
    result.add_argument("--server-binary", default="llama-server", help="Worker only: installed llama-server[.exe]")
    result.add_argument("--bind", default="127.0.0.1", help="LLM worker bind IP; explicit opt-in for LAN")
    result.add_argument("--port", type=int, default=18082, help="LLM worker port")
    result.add_argument("--gpu-layers", default="99", help="Worker only: GPU layer count")
    result.add_argument("--context", type=int, default=8192, help="Worker only: context tokens")
    result.add_argument("--threads", type=int, default=4, help="Worker only: CPU threads")
    return result


def read_env(path):
    """Read simple dotenv assignments, without evaluating code or expanding secrets."""
    values = {}
    if not path.exists():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key.replace("_", "a").isalnum():
            raise ValueError(f"Unsupported .env assignment at line {number}; use KEY=value")
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.rfind(quote)
            trailing = value[closing + 1:].strip()
            if closing == 0 or (trailing and not trailing.startswith("#")):
                raise ValueError(f"Unsupported multiline/quoted .env value at line {number}")
            value = value[1:closing]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if "${" in value:
            raise ValueError(f"Use a literal value or an OS environment variable at .env line {number}")
        values[key] = value
    return values


def absolute(path, root=ROOT):
    path = Path(path).expanduser()
    return path if path.is_absolute() else root / path


def enabled(env, name):
    return env.get(name, "false").lower() in {"true", "1", "yes", "on"}


def endpoint(value):
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ValueError("Invalid --llm-url / LLM_URL") from exc
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path.rstrip("/") != "/v1" or port == 0):
        raise ValueError("LLM URL must be http(s)://host[:port]/v1 without credentials, query or fragment")
    return url


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def environment(args, root=ROOT, inherited=None):
    inherited = dict(os.environ if inherited is None else inherited)
    # An inference-only worker never needs the app's database or CRM secrets.
    config_file = configured_env_file(root / ".env", environment=inherited)
    env = {**(read_env(config_file) if args.role == "app" and config_file else {}), **inherited}
    if args.role == "app":
        env["LLM_REMOTE"] = str(args.mode == "distributed").lower()
        if args.speech == "portable":
            env.update(STT_BACKEND="faster-whisper", NATURAL_TTS_ENABLED="false", LAN_ENABLED="false")
        elif args.speech == "mac-metal":
            env.update(STT_BACKEND="whisper.cpp", NATURAL_TTS_ENABLED="true")
            env.setdefault("WHISPER_METAL_MODEL", str(root / "models/whisper-turbo-metal.bin"))
        if args.no_telegram:
            env["CHANNEL_API_KEY"] = ""
        if args.model_dir or args.download_models or args.download_only:
            base = absolute(args.model_dir or "models", root)
            env.update(WHISPER_MODEL=str(base / "whisper-small"),
                       WHISPER_METAL_MODEL=str(base / "whisper-turbo-metal.bin"),
                       PIPER_MODEL=str(base / "ru_RU-denis-medium.onnx"),
                       NATURAL_TTS_MODEL=str(base / "qwen3-tts-base-0.6b"))
            if args.speech != "portable":
                env["VOICE_REFERENCE_DIR"] = str(base / "voices")
    if args.llm_url:
        env["LLM_URL"] = args.llm_url.rstrip("/")
    if args.model:
        env["LLM_MODEL"] = args.model
    if args.model_path or args.model_dir or args.download_models or args.download_only:
        path = args.model_path or (args.model_dir or Path("models")) / "qwen3-4b-q4.gguf"
        env["LLM_MODEL_PATH"] = str(absolute(path, root))
    return env


def read_token(path):
    token = path.expanduser().read_text(encoding="utf-8-sig").strip()
    if len(token) < 32 or any(character.isspace() for character in token):
        raise ValueError("The LLM key file must contain one token of at least 32 characters")
    return token


def worker_command(args, env, root=ROOT):
    try:
        ipaddress.ip_address(args.bind)
    except ValueError as exc:
        raise ValueError("--bind must be an explicit IPv4 or IPv6 address") from exc
    if not 1 <= args.port <= 65535 or args.context < 1 or args.threads < 1:
        raise ValueError("Invalid --port, --context or --threads")
    if args.gpu_layers not in {"auto", "all"} and not args.gpu_layers.isdecimal():
        raise ValueError("--gpu-layers must be a nonnegative integer, all or auto")
    if not args.llm_api_key_file:
        raise ValueError("LLM worker requires --llm-api-key-file (same token as the app)")
    alias = env.get("LLM_MODEL", "qwen3-4b")
    if not alias.strip() or "," in alias:
        raise ValueError("--model must contain one nonempty model alias")
    model = absolute(env.get("LLM_MODEL_PATH", "models/qwen3-4b-q4.gguf"), root)
    return [args.server_binary, "-m", str(model), "--host", args.bind, "--port", str(args.port),
            "-c", str(args.context), "-ngl", args.gpu_layers, "-t", str(args.threads),
            "--parallel", "1", "--alias", alias, "--jinja", "--chat-template-kwargs",
            '{"enable_thinking": false}', "--api-key-file", str(absolute(args.llm_api_key_file, root))]


def model_profile(args, env=None):
    if args.role == "llm":
        return "llm-worker"
    env = env or {}
    native = enabled(env, "NATURAL_TTS_ENABLED") or env.get("STT_BACKEND") == "whisper.cpp"
    return "mac-metal" if args.speech == "mac-metal" or (args.speech == "configured" and native) else "portable"


def download_command(args, root=ROOT, env=None, profile=None):
    command = [sys.executable, str(root / "scripts/fetch_models.py"), "--profile", profile or model_profile(args, env)]
    if args.model_dir:
        command += ["--model-dir", str(absolute(args.model_dir, root))]
    if args.role == "app" and args.mode == "distributed":
        command.append("--exclude-llm")
    return command


def download_commands(args, env):
    commands = [download_command(args, env=env)]
    if (args.role == "app" and model_profile(args, env) == "mac-metal"
            and env.get("STT_BACKEND", "faster-whisper") != "whisper.cpp"):
        commands.append(download_command(args, env=env, profile="portable"))
    return commands


def diagnostics(args, env, root=ROOT, system=None):
    """Pure/read-only preflight: no network, subprocess, service changes or model imports."""
    errors, notes = [], []
    system = system or platform.system()
    if not (3, 12) <= sys.version_info[:2] < (3, 14):
        errors.append("Use Python 3.12 or 3.13; wrappers prefer the project's .venv")
    if args.role == "llm":
        if not shutil.which(args.server_binary):
            errors.append("Install a compatible llama.cpp server binary and pass --server-binary PATH")
        model = absolute(env.get("LLM_MODEL_PATH", "models/qwen3-4b-q4.gguf"), root)
        if not model.is_file():
            errors.append(f"Missing GGUF: {model}; use --download-models")
        if args.llm_api_key_file:
            try:
                read_token(absolute(args.llm_api_key_file, root))
            except (OSError, ValueError):
                errors.append("Missing/invalid key file: create a private file with a 32+ character token")
        if not loopback(args.bind):
            notes.append("Worker is explicitly bound beyond loopback. Restrict firewall access to the app/VPN.")
        notes.append("GPU support depends on the installed llama.cpp build, drivers and VRAM; not probed here.")
        return errors, notes
    if system != "Darwin" and (enabled(env, "NATURAL_TTS_ENABLED") or env.get("STT_BACKEND") == "whisper.cpp"):
        errors.append("This profile requires macOS Metal/MLX. Explicitly choose --speech portable for CPU/Piper.")
    if system == "Darwin" and enabled(env, "NATURAL_TTS_ENABLED") and platform.machine() != "arm64":
        errors.append("Native MLX voice requires Apple Silicon; explicitly use --speech portable on Intel Mac")
    if system != "Darwin" and enabled(env, "LAN_ENABLED"):
        errors.append("Automatic LAN setup is macOS-only; set LAN_ENABLED=false and configure HTTPS manually")
    if args.speech == "portable":
        notes.append("Portable profile: CPU faster-whisper + Piper Denis; the approved female MLX voice is unavailable.")
    requirements = ["dotenv", "pydantic_settings", "fastapi", "uvicorn", "asyncpg", "httpx", "numpy", "piper",
                    "pipecat", "soxr", "num2words", "cryptography"]
    if env.get("STT_BACKEND", "faster-whisper") != "whisper.cpp":
        requirements.append("faster_whisper")
    if enabled(env, "NATURAL_TTS_ENABLED"):
        requirements += ["mlx", "mlx_audio"]
    if len(env.get("CHANNEL_API_KEY", "")) >= 16:
        requirements += ["telethon", "pytgcalls"]
    missing = [name for name in requirements if importlib.util.find_spec(name) is None]
    if missing:
        errors.append("Missing Python packages: " + ", ".join(missing) + "; see docs/LAUNCHERS.md")
    try:
        ZoneInfo("Europe/Minsk")
    except ZoneInfoNotFoundError:
        errors.append("Missing IANA timezone data; install tzdata (required on Windows)")
    if not shutil.which("docker"):
        errors.append("Install Docker with Compose and start its engine before application startup")
    if args.mode == "local" and not shutil.which("llama-server"):
        errors.append("Local mode requires llama-server in PATH; install llama.cpp or choose distributed")
    paths = [("Piper", env.get("PIPER_MODEL", "models/ru_RU-denis-medium.onnx"))]
    if env.get("STT_BACKEND", "faster-whisper") == "whisper.cpp":
        paths += [("Whisper Metal", env.get("WHISPER_METAL_MODEL", "models/whisper-small-metal.bin")),
                  ("Native Whisper library", ".runtime/libvoice_whisper.dylib")]
    else:
        base = env.get("WHISPER_MODEL", "models/whisper-small")
        paths += [("Whisper weights", str(Path(base) / "model.bin")),
                  ("Whisper config", str(Path(base) / "config.json"))]
    paths.append(("Piper config", paths[0][1] + ".json"))
    if args.mode == "local":
        paths.append(("LLM", env.get("LLM_MODEL_PATH", "models/qwen3-4b-q4.gguf")))
    if enabled(env, "NATURAL_TTS_ENABLED"):
        base = Path(env.get("NATURAL_TTS_MODEL", "models/qwen3-tts-base-0.6b"))
        refs = Path(env.get("VOICE_REFERENCE_DIR", "app/assets/voices"))
        paths += [("Natural TTS config", str(base / "config.json")),
                  ("Natural TTS tokenizer", str(base / "speech_tokenizer/model.safetensors")),
                  ("Voice metadata", str(refs / "metadata.json")),
                  ("Male voice reference", str(refs / "male.wav")),
                  ("Female voice reference", str(refs / "female.wav"))]
    for name, path in paths:
        if not absolute(path, root).is_file():
            errors.append(f"Missing {name}: {path}; use --download-models or configure its path")
    for name, length in [("ADMIN_PASSWORD", 16), ("OPERATOR_PASSWORD", 16),
                         ("MANAGEMENT_SECRET", 32), ("LLM_API_KEY", 16)]:
        if len(env.get(name, "")) < length:
            errors.append(f"Set {name} ({length}+ characters) in .env or the process environment")
    if enabled(env, "NATURAL_TTS_ENABLED") and len(env.get("NATURAL_TTS_API_KEY", "")) < 16:
        errors.append("Set NATURAL_TTS_API_KEY (16+ characters) for the native voice worker")
    if args.mode == "distributed":
        notes.append("Remote mode verifies /v1/models at startup and never starts a local LLM.")
    notes.append("Doctor is offline: it does not verify Docker readiness, remote reachability or end-to-end audio.")
    return errors, notes


def show_command(command):
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def interactive_arguments():
    print("Voice Desk: 1 local app; 2 app + remote LLM; 3 LLM worker; 4 download models; 5 doctor")
    choice = input("Choose [1-5, Enter=help]: ").strip()
    choices = {"1": [], "2": ["--mode", "distributed"], "3": ["--role", "llm"],
               "4": ["--download-only"], "5": ["--doctor"]}
    if choice not in choices:
        return ["--help"]
    argv = choices[choice]
    if choice == "3":
        argv += ["--llm-api-key-file", input("Private API key file: ").strip()]
    elif platform.system() != "Darwin":
        answer = input("CPU ASR + Piper Denis; female MLX voice unavailable. Accept portable profile [y/N]? ")
        if answer.lower() != "y":
            return ["--help"]
        argv += ["--speech", "portable"]
    if choice == "2":
        argv += ["--llm-url", input("Remote /v1 URL (or loopback tunnel URL): ").strip()]
    return argv


def main(argv=None):
    if argv is None and len(sys.argv) == 1:
        argv = interactive_arguments() if sys.stdin.isatty() else ["--help"]
    args = parser().parse_args(argv)
    try:
        env = environment(args)
        if args.download_only:
            command = []
        elif args.role == "app":
            address = endpoint(env.get("LLM_URL", "http://127.0.0.1:18082/v1"))
            if args.mode == "local" and (not loopback(address.hostname) or address.scheme != "http"):
                raise ValueError("Local mode requires a loopback http URL; use --mode distributed for a remote LLM")
            command = [sys.executable, str(ROOT / "scripts/run.py")]
        else:
            command = worker_command(args, env)
        print(f"Role: {args.role}; mode: {args.mode}; speech: {args.speech}")
        if args.download_models or args.download_only:
            for download in download_commands(args, env):
                print("Models: " + show_command(download))
        if not args.download_only:
            print("Launch: " + show_command(command))
        if args.role == "app" and not args.download_only:
            print("LLM endpoint: " + env.get("LLM_URL", "http://127.0.0.1:18082/v1"))
            print("Existing running applications do not change profile; stop them before switching modes.")
        if args.dry_run:
            print("Dry run: no downloads, network calls, services or file writes.")
            return 0
        if args.llm_api_key_file and args.role == "app":
            env["LLM_API_KEY"] = read_token(absolute(args.llm_api_key_file))
        if (args.role == "app" and not args.download_only and not args.doctor
                and platform.system() != "Darwin"
                and (enabled(env, "NATURAL_TTS_ENABLED") or env.get("STT_BACKEND") == "whisper.cpp")):
            raise ValueError("This profile needs macOS Metal/MLX; explicitly choose --speech portable")
        if (args.download_models or args.download_only) and not args.doctor:
            if args.model_path:
                raise ValueError("--download-models uses the catalog GGUF; use --model-dir, not --model-path")
            for download in download_commands(args, env):
                subprocess.run(download, cwd=ROOT, env=env, check=True)
            if args.download_only:
                return 0
        errors, notes = diagnostics(args, env)
        for note in notes:
            print("INFO: " + note)
        for error in errors:
            print("ERROR: " + error, file=sys.stderr)
        if errors:
            return 1
        if args.doctor:
            print("Offline preflight passed. Nothing was started or downloaded.")
            return 0
        # Environment overrides are inherited by every child started by run.py.
        runtime_env = build_child_environment("llm", inherited=env) if args.role == "llm" else env
        if args.role == "llm":
            from uuid import uuid4

            from scripts.runtime_manifest import collect_runtime_manifest, write_runtime_manifest

            manifest = collect_runtime_manifest(root=ROOT, config=env, role="llm")
            write_runtime_manifest(ROOT / "artifacts/runtime-manifests" / (uuid4().hex + ".json"), manifest)
            print("Runtime manifest: " + manifest["semantic_sha256"])
        return subprocess.call(command, cwd=ROOT, env=runtime_env)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        # Do not render arbitrary subprocess/stdout payloads or token values.
        print(f"Launcher error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
