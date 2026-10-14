"""Download attributed public MInDS-14 Russian audio for local diagnostic evaluation."""

import argparse
import hashlib
import json
import random
from pathlib import Path

import httpx

REPOSITORY = "PolyAI/minds14"
REVISION = "40ce77cb32a384e4d50a568e1ec39ac804019d33"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=84)
    parser.add_argument(
        "--directory", type=Path, default=Path.home() / ".cache/voice-desk/evaluation/minds14-ru"
    )
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with httpx.Client(timeout=45, follow_redirects=True, trust_env=False) as client:
        metadata = (
            client.get(
                "https://datasets-server.huggingface.co/first-rows",
                params={"dataset": REPOSITORY, "config": "ru-RU", "split": "train"},
            )
            .raise_for_status()
            .json()
        )
        names = next(f["type"]["names"] for f in metadata["features"] if f["name"] == "intent_class")
        all_rows = []
        offset = 0
        while offset < 420:
            batch = (
                client.get(
                    "https://datasets-server.huggingface.co/rows",
                    params={
                        "dataset": REPOSITORY,
                        "config": "ru-RU",
                        "split": "train",
                        "offset": offset,
                        "length": 100,
                    },
                )
                .raise_for_status()
                .json()
            )
            all_rows.extend(batch["rows"])
            if len(batch["rows"]) < 100:
                break
            offset += 100
        grouped = {label: [] for label in names}
        for row in all_rows:
            grouped[names[row["row"]["intent_class"]]].append(row)
        selected = []
        rng = random.Random(20261013)
        for label, rows in grouped.items():
            rng.shuffle(rows)
            selected.extend(rows[: max(2, args.limit // len(names))])
        selected = selected[: args.limit]
        manifest = []
        counts = {label: 0 for label in names}
        for item in selected:
            row = item["row"]
            label = names[row["intent_class"]]
            identity = hashlib.sha256((REVISION + row["path"]).encode()).hexdigest()[:24]
            path = directory / (identity + ".wav")
            if not path.exists():
                audio = client.get(row["audio"][0]["src"]).raise_for_status().content
                path.write_bytes(audio)
                path.chmod(0o600)
            # Public viewer follows repository HEAD: verify its revision still matches
            # the pinned version before accepting the downloaded corpus below.
            index = counts[label]
            counts[label] += 1
            manifest.append(
                {
                    "id": identity,
                    "audio": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "text": row["transcription"],
                    "intent": label,
                    "split": "dev" if index % 2 == 0 else "holdout",
                    "speaker": None,
                    "source_path": row["path"],
                }
            )
            print("Downloaded", len(manifest), "/", len(selected), flush=True)
        remote = client.get("https://huggingface.co/api/datasets/" + REPOSITORY).raise_for_status().json()
        if remote["sha"] != REVISION:
            raise RuntimeError("Dataset revision changed; pin and recheck before evaluation")
        out = {
            "dataset": REPOSITORY,
            "revision": REVISION,
            "source": "https://huggingface.co/datasets/PolyAI/minds14",
            "license": "CC-BY-4.0",
            "purpose": "public banking-domain diagnostic; not final acceptance of receptionist actions",
            "speaker_split_verified": False,
            "labels": names,
            "records": manifest,
        }
        (directory / "manifest.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        print("Manifest:", directory / "manifest.json", flush=True)


if __name__ == "__main__":
    main()
