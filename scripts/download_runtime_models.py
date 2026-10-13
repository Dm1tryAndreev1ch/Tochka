"""Install only the pinned weights required by the selected Mac runtime."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_models import ROOT, download, get_json  # noqa: E402


def main():
    path = ROOT / "model-manifest.json"
    records = json.loads(path.read_text()) if path.exists() else []
    specs = [
        (
            "unsloth/Qwen3-4B-Instruct-2507-GGUF",
            "a06e946bb6b655725eafa393f4a9745d460374c9",
            "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            "qwen3-4b-q4.gguf",
            "Apache-2.0",
        ),
        (
            "ggerganov/whisper.cpp",
            "5359861c739e955e79d9a303bcbc70fb988958b1",
            "ggml-large-v3-turbo-q5_0.bin",
            "whisper-turbo-metal.bin",
            "MIT",
        ),
        (
            "rhasspy/piper-voices",
            "c10ece1aade47bb51c153c893d14e5bf8e5b7117",
            "ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx",
            "ru_RU-denis-medium.onnx",
            "MIT; CC0 dataset",
        ),
        (
            "rhasspy/piper-voices",
            "c10ece1aade47bb51c153c893d14e5bf8e5b7117",
            "ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx.json",
            "ru_RU-denis-medium.onnx.json",
            "MIT; CC0 dataset",
        ),
    ]
    repo = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
    revision = "50f45ef0047cde7e84c2ef04326acb8ada2436a7"
    for file in get_json(f"https://huggingface.co/api/models/{repo}/revision/{revision}")["siblings"]:
        name = file["rfilename"]
        if not name.startswith(".") and name != "README.md":
            specs.append((repo, revision, name, "qwen3-tts-base-0.6b/" + name, "Apache-2.0"))
    for spec in specs:
        record = download(*spec)
        record["purpose"] = "runtime"
        records = [r for r in records if r["file"] != record["file"]] + [record]
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    print("Selected runtime weights verified")


if __name__ == "__main__":
    main()
