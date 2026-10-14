"""Pinned, resumable ASR asset downloads restricted to the external benchmark disk."""

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

EXTERNAL = Path("/Volumes/Dmitry_disk/voice-model-benchmarks")
MODELS = {
    "qwen-asr-0.6b": (
        "mlx-community/Qwen3-ASR-0.6B-8bit",
        "89e96d92ba34aca20b3e29fb10cc284097d1219f",
        "Apache-2.0",
        None,
    ),
    "qwen-asr-1.7b": (
        "mlx-community/Qwen3-ASR-1.7B-8bit",
        "a8379a2e2f9e313c9292cdf1af4055ab56d50d55",
        "Apache-2.0",
        None,
    ),
    "whisper-large-v3": (
        "ggerganov/whisper.cpp",
        "5359861c739e955e79d9a303bcbc70fb988958b1",
        "MIT",
        {"ggml-large-v3.bin"},
    ),
    "gigaam-v3-rnnt": (
        "aystream/GigaAM-v3-e2e-rnnt-mlx",
        "decfca492069ea30fb5ead79c4516d50c16d93ea",
        "MIT",
        None,
    ),
}


def external_directory(path):
    if not Path("/Volumes/Dmitry_disk").is_mount():
        raise FileNotFoundError("Required external volume is not mounted")
    path = Path(path).resolve()
    if not path.is_relative_to(EXTERNAL.resolve(strict=True)):
        raise ValueError("ASR assets must remain on the external benchmark disk")
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified(path, info):
    if not path.is_file() or path.stat().st_size != info["size"]:
        return False
    if info.get("lfs"):
        return sha256(path) == info["lfs"]["oid"]
    digest = hashlib.sha1(f"blob {info['size']}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == info["oid"]


def ranged_download(official, target, info, workers=32):
    """Only confirmed exact ranges enter the durable checkpoint; URLs stay in memory."""
    total, expected = info["size"], info["lfs"]["oid"]
    part = target.with_suffix(target.suffix + ".part")
    checkpoint = part.with_suffix(part.suffix + ".ranges-fast.json")
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text())
        if state["sha256"] != expected or state["bytes"] != total:
            raise ValueError("Checkpoint belongs to different pinned asset")
    else:
        if part.exists():
            raise ValueError("Unrecognized partial file retained")
        part.touch()
        state = {"range_bytes": 2 * 1024**2, "completed": [], "sha256": expected, "bytes": total}
        checkpoint.write_text(json.dumps(state))
    completed = set(state["completed"])
    lock, url_lock = threading.Lock(), threading.Lock()
    current_url = official

    def refresh_url():
        nonlocal current_url
        with url_lock:
            with urllib.request.urlopen(
                urllib.request.Request(official, method="HEAD"), timeout=60
            ) as response:
                current_url = response.geturl()

    refresh_url()
    spans = [
        (start, min(start + state["range_bytes"], total) - 1)
        for start in range(0, total, state["range_bytes"])
    ]
    started = time.monotonic()
    print(f"{target.parent.name}/{target.name}: resume {len(completed)}/{len(spans)} ranges", flush=True)

    def fetch(span):
        start, end = span
        if start in completed:
            return
        for attempt in range(5):
            try:
                request = urllib.request.Request(current_url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(request, timeout=90) as response, part.open("r+b") as output:
                    if (
                        response.status != 206
                        or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}"
                    ):
                        raise ValueError("Invalid range response")
                    output.seek(start)
                    count = 0
                    while chunk := response.read(256 * 1024):
                        count += len(chunk)
                        if count > end - start + 1:
                            raise ValueError("Excess range bytes")
                        output.write(chunk)
                    if count != end - start + 1:
                        raise ValueError("Truncated range")
                    output.flush()
                with lock:
                    state["completed"].append(start)
                    temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
                    temporary.write_text(json.dumps(state))
                    temporary.replace(checkpoint)
                    count = len(state["completed"])
                    if count % 64 == 0 or count == len(spans):
                        print(
                            f"{target.parent.name}: {count}/{len(spans)} ranges, {time.monotonic() - started:.1f}s",
                            flush=True,
                        )
                return
            except Exception as error:
                if attempt == 4:
                    raise RuntimeError(f"Range {start}: {type(error).__name__}") from None
                if isinstance(error, urllib.error.HTTPError) and error.code in (401, 403):
                    refresh_url()
                time.sleep(min(10, attempt + 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch, spans))
    if not verified(part, info):
        raise RuntimeError("Final asset SHA256 mismatch; checkpoint retained")
    part.replace(target)
    checkpoint.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--destination", type=Path, default=EXTERNAL / "stt-models")
    args = parser.parse_args()
    destination = external_directory(args.destination)
    manifest_path = destination / "manifest.json"
    records = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    for alias in args.models:
        repo, revision, license_name, selected = MODELS[alias]
        with urllib.request.urlopen(
            f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true", timeout=60
        ) as response:
            files = json.load(response)
        for info in files:
            filename = info["path"]
            if info["type"] != "file" or filename.startswith(".") or (selected and filename not in selected):
                continue
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid remote filename")
            target = external_directory(destination / alias) / relative
            if not target.resolve().is_relative_to(destination):
                raise ValueError("Destination symlink escapes external directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not verified(target, info):
                cached = Path.home() / ".cache/voice-desk/evaluation/gigaam-v3-rnnt" / filename
                if alias == "gigaam-v3-rnnt" and verified(cached, info):
                    shutil.copyfile(cached, target)
                else:
                    official = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}?download=true"
                    if info.get("lfs") and info["size"] > 8 * 1024**2:
                        ranged_download(official, target, info)
                    else:
                        part = target.with_suffix(target.suffix + ".part")
                        with (
                            urllib.request.urlopen(official, timeout=90) as response,
                            part.open("wb") as output,
                        ):
                            shutil.copyfileobj(response, output, 256 * 1024)
                        if not verified(part, info):
                            raise RuntimeError("Metadata blob verification failed")
                        part.replace(target)
            record = {
                "model": alias,
                "repo": repo,
                "revision": revision,
                "license": license_name,
                "file": filename,
                "path": str(target),
                "bytes": info["size"],
                "sha256": sha256(target),
                "source_oid": info.get("lfs", {}).get("oid", info["oid"]),
            }
            records = [r for r in records if (r["model"], r["file"]) != (alias, filename)] + [record]
            manifest_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
            print(f"Verified {alias}/{filename}", flush=True)


if __name__ == "__main__":
    main()
