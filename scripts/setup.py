"""Create private configuration and install the reproducible local stack."""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-models", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] not in [(3, 12), (3, 13)]:
        raise SystemExit("Use Python 3.12 or 3.13")
    free = shutil.disk_usage(ROOT).free / 1024**3
    print(
        f"Available: {free:.1f} GB. Selected model weights: approximately 4.8 GB, plus dependencies and containers."
    )
    if free < 10 and not (ROOT / "models/qwen3-4b-q4.gguf").exists():
        raise SystemExit("At least 10 GB free required for a fresh install")
    config = ROOT / ".env"
    if not config.exists():
        text = (ROOT / ".env.example").read_text()
        for placeholder in (
            "change-this-admin-password",
            "change-this-operator-password",
            "change-me-too",
            "change-me",
            "generate-a-random-32-byte-secret",
            "generate-a-random-llm-secret",
        ):
            text = text.replace(placeholder, secrets.token_urlsafe(32))
        text += "\nOPERATOR_PASSWORD=" + secrets.token_urlsafe(24) + "\n"
        text += (
            "\nNATURAL_TTS_API_KEY="
            + secrets.token_urlsafe(32)
            + "\nCHANNEL_API_KEY="
            + secrets.token_urlsafe(32)
            + "\n"
        )
        config.write_text(text)
        config.chmod(0o600)
    # Documents can be synchronized with iCloud: virtualenv belongs in local cache.
    venv = Path(os.environ.get("VOICE_VENV_PATH", Path.home() / ".cache/voice-desk/venv"))
    if not (venv / "bin/python").exists():
        run([sys.executable, "-m", "venv", str(venv)])
    link = ROOT / ".venv"
    if not link.exists():
        link.symlink_to(venv, target_is_directory=True)
    python = str(link / "bin/python")
    run([python, "-m", "pip", "install", "-r", "requirements.lock"])
    run(["brew", "install", "whisper.cpp", "ggml", "rubberband"])
    run([python, "scripts/build_whisper.py"])
    run(["docker", "compose", "up", "-d"])
    run([python, "scripts/setup_database.py"])
    run([python, "scripts/provision_crm.py"])
    if not args.no_models:
        run([python, "scripts/download_runtime_models.py"])
    print("Ready. Start: .venv/bin/python scripts/run.py")
    print("Passwords are in .env. Open the file locally; do not publish it.")


if __name__ == "__main__":
    main()
