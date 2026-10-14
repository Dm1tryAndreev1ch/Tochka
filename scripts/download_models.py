"""Download pinned local weights; stream to .part, verify against LFS SHA256."""

import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "models"


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def download_destination(destination=None):
    """Validate an explicit storage directory before any network or file writes."""
    if destination is None:
        return DEST
    directory = Path(destination).expanduser()
    if not directory.is_dir():
        raise FileNotFoundError(f"Model directory is unavailable: {directory}")
    directory = directory.resolve(strict=True)
    if directory.parts[:2] == ("/", "Volumes") and len(directory.parts) >= 3:
        volume = Path(*directory.parts[:3])
        if not volume.is_mount():
            raise FileNotFoundError(f"External volume is not mounted: {volume}")
    return directory


def download(repo, revision, filename, local, license_name, *, destination=None):
    directory = download_destination(destination)
    relative = Path(local)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Model filename must remain inside its destination")
    target = directory / relative
    part = target.with_suffix(target.suffix + ".part")
    if destination is not None and any(
        not candidate.resolve().is_relative_to(directory) for candidate in (target, part)
    ):
        raise ValueError("Model destination symlink escapes the selected directory")
    info = get_json(
        f"https://huggingface.co/api/models/{repo}/tree/{revision}/{filename.rsplit('/', 1)[0] if '/' in filename else ''}?expand=true"
    )
    fileinfo = next(item for item in info if item.get("path") == filename)
    expected = fileinfo.get("lfs", {}).get("oid")
    if destination is not None:
        download_destination(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if (
        target.exists()
        and expected
        and hashlib.file_digest(target.open("rb"), "sha256").hexdigest() == expected
    ):
        print(f"Verified {local}", flush=True)
    else:
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"
        digest = hashlib.sha256()
        print(f"Downloading {local}: {fileinfo['size'] / 1024**2:.0f} MB", flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if expected and digest.hexdigest() != expected:
            part.unlink()
            raise RuntimeError(f"SHA256 mismatch: {local}")
        part.replace(target)
    with target.open("rb") as file:
        sha = hashlib.file_digest(file, "sha256").hexdigest()
    return {
        "file": local,
        "repo": repo,
        "revision": revision,
        "source_file": filename,
        "sha256": sha,
        "bytes": target.stat().st_size,
        "license": license_name,
        "path": str(target.resolve()),
    }


def main():
    DEST.mkdir(exist_ok=True)
    free = shutil.disk_usage(ROOT).free
    print(f"Free disk: {free / 1024**3:.1f} GB; new weights approximately 3.1 GB", flush=True)
    if free < 4 * 1024**3:
        raise RuntimeError("At least 4 GB free required")
    manifest = []
    specs = [
        (
            "Systran/faster-whisper-small",
            "536b0662742c02347bc0e980a01041f333bce120",
            "model.bin",
            "whisper-small/model.bin",
            "MIT",
        ),
        (
            "Systran/faster-whisper-small",
            "536b0662742c02347bc0e980a01041f333bce120",
            "config.json",
            "whisper-small/config.json",
            "MIT",
        ),
        (
            "Systran/faster-whisper-small",
            "536b0662742c02347bc0e980a01041f333bce120",
            "tokenizer.json",
            "whisper-small/tokenizer.json",
            "MIT",
        ),
        (
            "Systran/faster-whisper-small",
            "536b0662742c02347bc0e980a01041f333bce120",
            "vocabulary.txt",
            "whisper-small/vocabulary.txt",
            "MIT",
        ),
        (
            "rhasspy/piper-voices",
            "c10ece1aade47bb51c153c893d14e5bf8e5b7117",
            "ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx",
            "ru_RU-denis-medium.onnx",
            "MIT (model repository); CC0 (dataset per MODEL_CARD)",
        ),
        (
            "rhasspy/piper-voices",
            "c10ece1aade47bb51c153c893d14e5bf8e5b7117",
            "ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx.json",
            "ru_RU-denis-medium.onnx.json",
            "MIT (model repository); CC0 (dataset per MODEL_CARD)",
        ),
    ]
    llm_repo = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
    llm_revision = "a06e946bb6b655725eafa393f4a9745d460374c9"
    llm_file = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
    specs.append((llm_repo, llm_revision, llm_file, "qwen3-4b-q4.gguf", "Apache-2.0"))
    for spec in specs:
        manifest.append(download(*spec))
        (ROOT / "model-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    for repo, filename, local in [
        ("rhasspy/piper-voices", "ru/ru_RU/denis/medium/MODEL_CARD", "piper-denis-MODEL_CARD.txt"),
        ("unsloth/Qwen3-4B-Instruct-2507-GGUF", "README.md", "qwen-model-card.md"),
        ("Systran/faster-whisper-small", "README.md", "whisper-model-card.md"),
    ]:
        revision = next(x["revision"] for x in manifest if x["repo"] == repo)
        with urllib.request.urlopen(
            f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"
        ) as response:
            (ROOT / "licenses" / local).write_bytes(response.read())
    print("Local weights ready", flush=True)


if __name__ == "__main__":
    main()
