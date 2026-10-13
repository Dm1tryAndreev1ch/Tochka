"""Pinned weights for the Mac voice profile, verified against upstream LFS hashes."""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_models import ROOT, download, get_json

SPECS = [
    (
        "ggerganov/whisper.cpp",
        "5359861c739e955e79d9a303bcbc70fb988958b1",
        "ggml-small.bin",
        "whisper-small-metal.bin",
        "MIT",
    ),
    (
        "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
        "049ef77fe8816b536193c0c25f9a214d17921282",
        None,
        "qwen3-tts-0.6b-8bit",
        "Apache-2.0",
    ),
]


def main():
    if shutil.disk_usage(ROOT).free < 3 * 1024**3:
        raise RuntimeError("At least 3 GB free is required for new weights")
    manifest_path = ROOT / "model-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    for repo, rev, file, local, license_name in SPECS:
        files = (
            [file]
            if file
            else [
                f["rfilename"]
                for f in get_json(f"https://huggingface.co/api/models/{repo}/revision/{rev}")["siblings"]
                if not f["rfilename"].startswith(".") and f["rfilename"] != "README.md"
            ]
        )
        for filename in files:
            record = download(repo, rev, filename, local if file else local + "/" + filename, license_name)
            manifest = [r for r in manifest if r["file"] != record["file"]] + [record]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("Voice upgrade weights verified", flush=True)


if __name__ == "__main__":
    main()
