"""Pinned multilingual voice design and reference-conditioned speech weights."""

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_models import ROOT, download, get_json

specs = [
    (
        "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "5c390979e4b93af5f2932f90742ca99c7dd04687",
        "qwen3-voice-design-1.7b",
    ),
    (
        "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
        "50f45ef0047cde7e84c2ef04326acb8ada2436a7",
        "qwen3-tts-base-0.6b",
    ),
]
path = ROOT / "model-manifest.json"
manifest = json.loads(path.read_text())
for repo, revision, local in specs:
    info = get_json(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true")
    for file in info["siblings"]:
        name = file["rfilename"]
        if name.startswith(".") or name == "README.md":
            continue
        target = ROOT / "models" / local / name
        if name.startswith("speech_tokenizer/"):
            source = ROOT / "models/qwen3-tts-0.6b-8bit" / name
            expected = file.get("lfs", {}).get("sha256") or file.get("lfs", {}).get("oid")
            if source.exists() and expected:
                with source.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual == expected:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        os.link(source, target)
        record = download(repo, revision, name, local + "/" + name, "Apache-2.0")
        manifest = [r for r in manifest if r["file"] != record["file"]] + [record]
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
print("Reference voice models verified")
