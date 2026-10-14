"""Start project dependencies and supervise the local services owned by this launcher."""

import asyncio
import ipaddress
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONPYCACHEPREFIX", str(Path.home() / ".cache/voice-desk/pycache"))


class LLMConfigurationError(RuntimeError):
    """A responding LLM server does not match the requested configuration."""


class LLMNotReady(RuntimeError):
    """A responding LLM server is still loading its model."""


class _NoLLMRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token to a redirect destination.


def _local_llm_url(url):
    try:
        address = urllib.parse.urlsplit(url)
        address.port  # Validate before constructing a command or performing network I/O.
    except ValueError as exc:
        raise LLMConfigurationError("LLM_URL is invalid") from exc
    hostname = address.hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    return address, loopback


def llm_model_path(config, root=ROOT):
    path = Path(config.llm_model_path).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def build_llama_command(config, key_file, cors_origin, root=ROOT):
    """Build the local server command from the same settings used by the gateway."""
    address, local = _local_llm_url(config.llm_url)
    if not local or address.scheme != "http" or address.path.rstrip("/") != "/v1":
        raise LLMConfigurationError("Automatic LLM startup requires a loopback http://host:port/v1 URL")
    model = llm_model_path(config, root)
    if not model.is_file():
        raise LLMConfigurationError(f"LLM_MODEL_PATH does not exist: {model}. Download the configured GGUF first.")
    gpu_layers = config.llm_gpu_layers
    if gpu_layers not in {"all", "auto"} and not gpu_layers.isdecimal():
        raise LLMConfigurationError("LLM_GPU_LAYERS must be a nonnegative integer, all, or auto")
    if not config.llm_model.strip() or "," in config.llm_model:
        raise LLMConfigurationError("LLM_MODEL must contain one nonempty model alias")
    return [
        "llama-server", "-m", str(model),
        "--host", address.hostname, "--port", str(address.port or 80),
        "-c", str(config.llm_context_tokens), "-ngl", gpu_layers, "-t", str(config.llm_threads),
        "--parallel", "1", "--alias", config.llm_model,
        "--jinja", "--chat-template-kwargs", json.dumps({"enable_thinking": config.llm_thinking}),
        "--api-key-file", str(key_file), "--cors-origins", cors_origin,
    ]


def check_llm_runtime(config, root=ROOT, opener=None):
    """Validate an existing server without taking ownership of its processes."""
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoLLMRedirect(),
    ).open
    base = config.llm_url.rstrip("/")
    headers = {"Authorization": "Bearer " + config.llm_api_key}

    def read_json(url):
        with opener(urllib.request.Request(url, headers=headers), timeout=2) as response:
            try:
                return json.load(response)
            except (ValueError, UnicodeError) as exc:
                raise LLMConfigurationError("LLM server returned invalid JSON") from exc

    try:
        models = read_json(base + "/models")
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise LLMNotReady("LLM server is still loading its model") from exc
        raise LLMConfigurationError(f"LLM /models returned HTTP {exc.code}; check LLM_URL and LLM_API_KEY") from exc
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise LLMConfigurationError("LLM /models did not return a model list")
    aliases = {
        model["id"] for model in models["data"]
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    }
    if config.llm_model not in aliases:
        raise LLMConfigurationError(
            f"LLM server does not serve LLM_MODEL={config.llm_model}. "
            "Restart the server with the configured model; the existing service was left running."
        )

    address, local = _local_llm_url(base)
    if config.llm_remote or not local or address.path != "/v1":
        return
    try:
        props = read_json(urllib.parse.urlunsplit(address._replace(path="/props", query="", fragment="")))
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 405}:
            return  # Other OpenAI-compatible servers may not expose llama.cpp properties.
        raise LLMConfigurationError(f"LLM /props returned HTTP {exc.code}") from exc
    except OSError as exc:
        raise LLMConfigurationError("LLM /models responded, but its /props could not be checked") from exc
    if not isinstance(props, dict):
        raise LLMConfigurationError("LLM /props did not return an object")
    actual_path = props.get("model_path")
    if actual_path:
        if not isinstance(actual_path, str):
            raise LLMConfigurationError("LLM /props returned an invalid model_path")
        actual = Path(actual_path).expanduser()
        actual = (actual if actual.is_absolute() else root / actual).resolve()
        if actual != llm_model_path(config, root):
            raise LLMConfigurationError(
                "The existing LLM server uses a different GGUF than LLM_MODEL_PATH. "
                "Restart it with the configured model; the existing service was left running."
            )


def ensure_dependencies():
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for PostgreSQL/EspoCRM")
    daemon = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if daemon.returncode and shutil.which("orbctl"):
        subprocess.run(["orbctl", "start"], check=True, timeout=90)
    subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=ROOT, check=True, timeout=120)


async def check_database_runtime(database_url):
    import asyncpg

    from app.database_security import assert_runtime_role
    from app.migration_runner import assert_current_schema

    conn = await asyncpg.connect(database_url, timeout=5, command_timeout=15)
    try:
        await assert_runtime_role(conn)
        await assert_current_schema(conn)
    finally:
        await conn.close()


def main():
    from dotenv import dotenv_values

    from app.config import Settings
    from app.runtime_environment import build_child_environment, configured_env_file
    from app.supervisor import ProcessSupervisor
    from scripts.runtime_manifest import collect_runtime_manifest, write_runtime_manifest

    config_file = configured_env_file(ROOT / ".env")
    env = {**(dotenv_values(config_file) if config_file else {}), **os.environ}
    if env.get("LAN_ENABLED", "false").lower() == "true" and shutil.which("ipconfig"):
        current_ip = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True).stdout.strip()
        if current_ip and current_ip != env.get("LAN_IP"):
            if config_file != ROOT / ".env":
                raise RuntimeError("Automatic LAN refresh requires the default configuration file; update the selected profile explicitly")
            subprocess.run([sys.executable, str(ROOT / "scripts/setup_lan.py"), "--ip", current_ip], check=True)
            env = {**dotenv_values(config_file), **os.environ}
    config = Settings(_env_file=None, **{key.lower(): value for key, value in env.items() if value is not None})
    if config.llm_remote:
        try:
            check_llm_runtime(config)
        except (OSError, urllib.error.URLError, LLMNotReady, LLMConfigurationError) as exc:
            raise RuntimeError("Remote LLM is unavailable or incompatible; no local LLM will be started") from exc
    ensure_dependencies()
    asyncio.run(check_database_runtime(config.database_url))
    child_settings = config.model_dump()
    manifest = collect_runtime_manifest(root=ROOT, config=child_settings)
    manifest_file = ROOT / "artifacts" / "runtime-manifests" / (uuid4().hex + ".json")
    write_runtime_manifest(manifest_file, manifest)
    print("Runtime manifest: " + manifest["semantic_sha256"])
    host, port = env.get("HOST", "127.0.0.1"), env.get("PORT", "7860")
    base = "http://127.0.0.1:" + port
    try:
        with urllib.request.urlopen(base + "/api/v1/health/live", timeout=2):
            pass
    except (OSError, urllib.error.URLError):
        pass
    else:
        try:
            check_llm_runtime(config)
        except (OSError, urllib.error.URLError, LLMNotReady):
            pass
        try:
            with urllib.request.urlopen(base + "/api/v1/health/ready", timeout=5) as response:
                state = json.load(response)
            print("Приложение уже запущено; состояние: " + state["status"])
        except (OSError, ValueError, urllib.error.URLError):
            print("Сервер запущен, но часть служб не готова. Проверьте состояние в кабинете; зависимости проекта восстановлены.")
        return

    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    supervisor = ProcessSupervisor(runtime, cwd=ROOT)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def start_web(name, module, service_port, bind_host="127.0.0.1"):
        return supervisor.start(name, [sys.executable, "-m", "uvicorn", module + ":app", "--host", bind_host, "--port", str(service_port), "--no-access-log"], ROOT / "artifacts" / (name + ".log"), env=build_child_environment(name, child_settings, inherited=env))

    try:
        try:
            check_llm_runtime(config)
            print("Using the existing LLM server: " + config.llm_model)
        except LLMNotReady as exc:
            raise RuntimeError("The existing LLM server is loading. Retry after it is ready.") from exc
        except (OSError, urllib.error.URLError):
            if config.llm_remote:
                raise RuntimeError("Remote LLM disconnected; no local LLM will be started")
            if not shutil.which("llama-server"):
                raise RuntimeError("Install llama.cpp first: brew install llama.cpp")
            key_file = runtime / "llm-key"
            command = build_llama_command(config, key_file, base)
            key_file.write_text(config.llm_api_key + "\n")
            key_file.chmod(0o600)
            supervisor.start("llm", command, ROOT / "artifacts" / "llama.log", env=build_child_environment("llm", inherited=env))
            for _ in range(90):
                supervisor.tick()
                try:
                    check_llm_runtime(config)
                    break
                except (OSError, urllib.error.URLError, LLMNotReady):
                    time.sleep(1)
            else:
                raise RuntimeError("LLM readiness timeout; see artifacts/llama.log")

        if len(env.get("CHANNEL_API_KEY", "")) >= 16:
            start_web("telegram-worker", "app.telegram_worker", 18084)
        if env.get("NATURAL_TTS_ENABLED", "false").lower() == "true":
            start_web("natural-tts", "app.natural_tts", 18083)
        if env.get("LAN_ENABLED", "false").lower() == "true":
            if not shutil.which("caddy"):
                raise RuntimeError("Install native Caddy for LAN HTTPS")
            supervisor.start("caddy", ["caddy", "run", "--config", str(runtime / "Caddyfile"), "--adapter", "caddyfile"], ROOT / "artifacts" / "caddy.log", env=build_child_environment("caddy", child_settings, inherited=env))
            start_web("mobile-setup", "app.mobile_setup", 8765, env["LAN_IP"])
        supervisor.start("gateway", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", port, "--no-access-log", "--ws-max-size", "4096"], ROOT / "artifacts" / "gateway.log", env=build_child_environment("gateway", child_settings, inherited=env))
        (runtime / "launcher.pid").write_text(str(os.getpid()))
        print(f"Client: {base} · Operator: {base}/operator", flush=True)
        while True:
            supervisor.tick()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.close()
        pid_file = runtime / "launcher.pid"
        if pid_file.exists() and pid_file.read_text() == str(os.getpid()):
            pid_file.unlink()


if __name__ == "__main__":
    main()
