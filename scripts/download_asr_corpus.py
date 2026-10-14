"""Download official Russian FLEURS dev/test archives with pinned source hashes."""

import json
import shutil
import urllib.request

from download_stt_candidates import EXTERNAL, external_directory, ranged_download, sha256, verified

REPOSITORY = "google/fleurs"
REVISION = "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
FILES = {
    "dev.tsv": {"size": 316328, "oid": "8e6be8c301fcdf2df3a99219c9c6ab643cf10e36"},
    "test.tsv": {"size": 735258, "oid": "1b1b37112dcbc6089433084255785f03f9fce445"},
    "dev.tar.gz": {
        "size": 187377526,
        "lfs": {"oid": "4dc533cd0f312f749bd6d0fe968f3183288d938e07ed97dc141d9f10570457f9"},
    },
    "test.tar.gz": {
        "size": 433142634,
        "lfs": {"oid": "8a0d6a0d23c3421f50c575bcf65d875cd19dadae2f7cabb415024f81b65178b1"},
    },
}


def main():
    root = external_directory(EXTERNAL / "corpora/fleurs-ru-v3")
    manifest = []
    for name, info in FILES.items():
        source = f"data/ru_ru/{'audio/' if name.endswith('.gz') else ''}{name}"
        official = f"https://huggingface.co/datasets/{REPOSITORY}/resolve/{REVISION}/{source}?download=true"
        target = root / name
        if not verified(target, info):
            if info.get("lfs"):
                ranged_download(official, target, info, workers=16)
            else:
                part = target.with_suffix(target.suffix + ".part")
                with urllib.request.urlopen(official, timeout=60) as response, part.open("wb") as output:
                    shutil.copyfileobj(response, output, 256 * 1024)
                if not verified(part, info):
                    raise ValueError("Corpus metadata Git blob hash mismatch")
                part.replace(target)
        manifest.append(
            {
                "dataset": REPOSITORY,
                "revision": REVISION,
                "license": "CC-BY-4.0",
                "file": source,
                "path": str(target),
                "bytes": info["size"],
                "sha256": sha256(target),
            }
        )
        (root / "download-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Verified official FLEURS {name}", flush=True)


if __name__ == "__main__":
    main()
