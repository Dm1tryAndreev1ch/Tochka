"""Run local Metal LLM and gateway; Ctrl+C stops only these child processes."""

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Compiled Python caches must stay local even when Documents is managed by iCloud.
os.environ.setdefault("PYTHONPYCACHEPREFIX", str(Path.home() / ".cache/voice-desk/pycache"))


def main():
    if not shutil.which("llama-server"):
        raise SystemExit("Install llama.cpp first: brew install llama.cpp")
    if not (ROOT / "models/qwen3-4b-q4.gguf").exists():
        raise SystemExit("Run scripts/setup.py first")
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:" + env.get("PORT", "7860") + "/api/v1/health/live", timeout=2
        ) as response:
            if response.status == 200:
                print("Приложение уже запущено: http://127.0.0.1:" + env.get("PORT", "7860"))
                return
    except Exception:
        pass
    from urllib.request import Request

    def check_llm():
        request = Request(
            env["LLM_URL"] + "/models", headers={"Authorization": "Bearer " + env.get("LLM_API_KEY", "")}
        )
        return urllib.request.urlopen(request, timeout=2)

    children = []
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        (ROOT / "artifacts").mkdir(exist_ok=True)
        llm_log = (ROOT / "artifacts/llama.log").open("a")
        try:
            check_llm().close()
            print("Using an existing local LLM server")
        except Exception:
            key_file = runtime / "llm-key"
            key_file.write_text(env.get("LLM_API_KEY", "") + "\n")
            key_file.chmod(0o600)
            children.append(
                subprocess.Popen(
                    [
                        "llama-server",
                        "-m",
                        str(ROOT / "models/qwen3-4b-q4.gguf"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18082",
                        "-c",
                        "8192",
                        "-ngl",
                        "99",
                        "-t",
                        "4",
                        "--parallel",
                        "1",
                        "--alias",
                        "qwen3-4b",
                        "--api-key-file",
                        str(key_file),
                        "--cors-origins",
                        "http://127.0.0.1:7860",
                    ],
                    cwd=ROOT,
                    stdout=llm_log,
                    stderr=subprocess.STDOUT,
                )
            )
            for attempt in range(90):
                if children[0].poll() is not None:
                    raise RuntimeError("LLM failed; see artifacts/llama.log")
                try:
                    check_llm().close()
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError("LLM readiness timeout")
        host, port = env.get("HOST", "127.0.0.1"), env.get("PORT", "7860")
        if len(env.get("CHANNEL_API_KEY", "")) >= 16:
            telegram_log = (ROOT / "artifacts/telegram-worker.log").open("a")
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.telegram_worker:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18084",
                        "--no-access-log",
                    ],
                    cwd=ROOT,
                    stdout=telegram_log,
                    stderr=subprocess.STDOUT,
                )
            )
        if env.get("NATURAL_TTS_ENABLED", "false").lower() == "true":
            natural_log = (ROOT / "artifacts/natural-tts.log").open("a")
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.natural_tts:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18083",
                        "--no-access-log",
                    ],
                    cwd=ROOT,
                    stdout=natural_log,
                    stderr=subprocess.STDOUT,
                )
            )
        if env.get("LAN_ENABLED", "false").lower() == "true":
            if not shutil.which("caddy"):
                raise RuntimeError("Install native Caddy for LAN HTTPS")
            caddy_log = (ROOT / "artifacts/caddy.log").open("a")
            children.append(
                subprocess.Popen(
                    ["caddy", "run", "--config", str(runtime / "Caddyfile"), "--adapter", "caddyfile"],
                    cwd=ROOT,
                    stdout=caddy_log,
                    stderr=subprocess.STDOUT,
                )
            )
            lan_log = (ROOT / "artifacts/mobile-setup.log").open("a")
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.mobile_setup:app",
                        "--host",
                        env["LAN_IP"],
                        "--port",
                        "8765",
                        "--no-access-log",
                    ],
                    cwd=ROOT,
                    stdout=lan_log,
                    stderr=subprocess.STDOUT,
                )
            )
        gateway = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                host,
                "--port",
                port,
                "--no-access-log",
                "--ws-max-size",
                "4096",
            ],
            cwd=ROOT,
        )
        children.append(gateway)
        (runtime / "launcher.pid").write_text(str(os.getpid()))
        print(f"Client: http://127.0.0.1:{port} · Operator: http://127.0.0.1:{port}/operator", flush=True)
        gateway.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
        pid_file = runtime / "launcher.pid"
        if pid_file.exists() and pid_file.read_text() == str(os.getpid()):
            pid_file.unlink()


if __name__ == "__main__":
    main()
