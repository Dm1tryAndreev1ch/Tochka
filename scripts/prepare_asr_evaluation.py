"""Freeze unseen licensed human FLEURS audio and browser/G.711 conditions before inference."""

import argparse
import csv
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from asr_metrics import tokens
from download_stt_candidates import EXTERNAL, external_directory, sha256
from scipy.signal import butter, sosfilt

REVISION = "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
SEED = 20261013
PROTOCOL = {
    "version": 3,
    "date": "2026-10-13",
    "language": "ru",
    "seed": SEED,
    "test_base_recordings": 200,
    "dev_base_recordings": 60,
    "channels": ["browser16k", "phone8k_pcmu", "phone8k_pcma"],
    "primary_conditions": ["complex"],
    "diagnostic_conditions": ["clean"],
    "complex": {
        "snr_db": 15,
        "echo_delay_s": 0.12,
        "echo_gain": 0.15,
        "noise": "alternating stationary and four-person babble from unselected official dev recordings",
        "tempo": "1.15 atempo for every fourth base",
        "pause": "0.35 seconds in a low-energy gap for every fourth base with index modulo 4 = 1",
    },
    "phone": {
        "bandpass_hz": [300, 3400],
        "filter": "scipy Butterworth order4 causal",
        "downsample": "soxr HQ float32 16k to 8k",
        "codec": "exact ffmpeg pcm_mulaw or pcm_alaw encode/decode",
        "upsample": "production soxr.ResampleStream 8000->16000 mono int16 default HQ, 20ms chunks + final flush",
    },
    "browser": {"format": "mono signed PCM16 16000 Hz", "conversion": "round and saturate"},
    "source_audio": "official original FLEURS WAV, resample soxr HQ to 16000 if required; TSV num_samples counts at 16000 Hz",
    "scoring": {
        "primary": "sum(S+D+I)/sum(reference words), basic Unicode normalization, preserve numeric values",
        "raw": "case/punctuation preserving whitespace tokens",
        "errors": "empty hypothesis, all words deleted",
        "threshold": "WER <= 0.06, integer comparison 100*errors <= 6*words",
        "critical_numbers": "literal numeric-token S/D/I plus exact sequence; no word-number rewrite",
    },
    "split": "official FLEURS train/dev vs test speakers disjoint per paper; no speaker IDs released, count unknown",
    "limitations": [
        "read public encyclopedia speech, not receptionist task acceptance",
        "controlled acoustics/codec simulation, not physical microphone or live SIP network",
        "normalization variants are not independent people; confidence intervals cluster by base recording",
        "FLEURS publication/pretraining contamination cannot be ruled out; unseen means unseen in this local project",
        "R-NLU-94 requires separately labelled human task utterances and actual ASR->NLU",
    ],
}


def stable_key(text):
    return hashlib.sha256(f"{SEED}:{text}".encode()).hexdigest()


def select_rows(root, split, count, excluded):
    with (root / f"{split}.tsv").open() as stream:
        # FLEURS TSV has literal quotes, not CSV-escaped fields.
        rows = list(csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
    if any(len(row) != 7 for row in rows):
        raise ValueError("Unexpected official TSV schema")
    candidates = sorted(
        (r for r in rows if int(r[0]) not in excluded and 2 <= int(r[5]) / 16000 <= 19.5),
        key=lambda r: stable_key(r[1]),
    )
    selected, sentence_ids, genders = [], set(), {"MALE": 0, "FEMALE": 0}
    for row in candidates:
        if row[0] in sentence_ids or row[6] not in genders or genders[row[6]] >= count // 2:
            continue
        selected.append(row)
        sentence_ids.add(row[0])
        genders[row[6]] += 1
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"Not enough eligible unique/gender-balanced {split} recordings")
    return selected


def ffmpeg_pcm(data, *options):
    process = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", *options],
        input=data,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return process.stdout


def g711(audio, codec):
    band = sosfilt(butter(4, [300, 3400], fs=16000, btype="bandpass", output="sos"), audio)
    narrow = np.clip(np.rint(soxr.resample(band, 16000, 8000) * 32768), -32768, 32767).astype("<i2")
    format_name = "mulaw" if codec == "pcmu" else "alaw"
    encoded = ffmpeg_pcm(
        narrow.tobytes(),
        "-f",
        "s16le",
        "-ar",
        "8000",
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-f",
        format_name,
        "pipe:1",
    )
    decoded = ffmpeg_pcm(
        encoded, "-f", format_name, "-ar", "8000", "-ac", "1", "-i", "pipe:0", "-f", "s16le", "pipe:1"
    )
    pcm = np.frombuffer(decoded, dtype="<i2")
    stream = soxr.ResampleStream(8000, 16000, 1, dtype="int16")
    chunks = [stream.resample_chunk(pcm[start : start + 160]) for start in range(0, len(pcm), 160)]
    chunks.append(stream.resample_chunk(np.zeros(0, dtype=np.int16), last=True))
    return np.concatenate(chunks).astype(np.float32) / 32768


def complex_audio(audio, index, babble):
    rng = np.random.default_rng(SEED + index)
    result = audio.copy()
    if index % 4 == 0:
        raw = ffmpeg_pcm(
            result.astype("<f4").tobytes(),
            "-f",
            "f32le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-af",
            "atempo=1.15",
            "-f",
            "f32le",
            "pipe:1",
        )
        result = np.frombuffer(raw, dtype="<f4").copy()
    elif index % 4 == 1:
        choices = range(16000, len(result) - 16000, 320)
        if choices:
            at = min(choices, key=lambda p: float(np.mean(result[p : p + 320] ** 2)))
            if np.mean(result[at : at + 320] ** 2) < 0.0003:
                result = np.concatenate([result[:at], np.zeros(5600, dtype=np.float32), result[at:]])
    offset = 1920
    echo = np.zeros(len(result) + offset, dtype=np.float32)
    echo[: len(result)] = result
    echo[offset:] += 0.15 * result
    noise = (
        rng.standard_normal(len(echo)).astype(np.float32)
        if index % 2 == 0
        else np.resize(babble, len(echo)).copy()
    )
    signal_rms = np.sqrt(np.mean(result**2))
    noise *= signal_rms / (10 ** (15 / 20) * max(1e-8, np.sqrt(np.mean(noise**2))))
    return np.clip(echo + noise, -1, 1), "stationary" if index % 2 == 0 else "babble"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=EXTERNAL / "corpora/fleurs-ru-v3")
    args = parser.parse_args()
    root = external_directory(args.corpus)
    manifest = root / "frozen-manifest-v3.json"
    if manifest.exists():
        raise FileExistsError("Frozen manifest retained; choose a new protocol directory")
    exclusions = []
    excluded = set()
    for split in ["dev", "holdout"]:
        path = Path.home() / ".cache/voice-desk/evaluation/fleurs-ru" / f"{split}.json"
        records = json.loads(path.read_text())["records"]
        exclusions.append({"source": str(path), "sha256": sha256(path), "records": len(records)})
        excluded.update(int(row["id"].rsplit("-", 1)[1]) for row in records)
    selected = {
        split: select_rows(root, split, count, excluded) for split, count in [("dev", 60), ("test", 200)]
    }
    donors = select_rows(root, "dev", 100, excluded)
    used = {r[0] for r in selected["dev"]}
    donors = [r for r in donors if r[0] not in used][:8]
    if len(donors) != 8:
        raise ValueError("Insufficient unselected babble donors")
    needed = {r[1] for rows in selected.values() for r in rows} | {r[1] for r in donors}
    original = external_directory(root / "original")
    for split in ["dev", "test"]:
        with tarfile.open(root / f"{split}.tar.gz", "r:gz") as archive:
            for member in archive:
                filename = Path(member.name).name
                if member.isfile() and filename in needed:
                    with archive.extractfile(member) as source, (original / filename).open("wb") as output:
                        import shutil

                        shutil.copyfileobj(source, output)
    donor_audio = []
    for donor in donors:
        signal, rate = sf.read(original / donor[1], dtype="float32")
        donor_audio.append(soxr.resample(signal, rate, 16000).astype(np.float32))
    babble = sum(np.resize(signal, 30 * 16000) for signal in donor_audio[:4]) / 4
    records = []
    for split, rows in selected.items():
        for index, row in enumerate(rows):
            base = f"fleurs-{split}-{row[0]}-{Path(row[1]).stem}"
            source = original / row[1]
            audio, rate = sf.read(source, dtype="float32")
            if (
                rate not in (16000, 24000, 48000)
                or audio.ndim != 1
                or abs(len(audio) / rate - int(row[5]) / 16000) > 1 / 16000
            ):
                raise ValueError("Unexpected original audio format/length")
            if rate != 16000:
                audio = soxr.resample(audio, rate, 16000).astype(np.float32)
            complicated, noise = complex_audio(audio, index + (1000 if split == "test" else 0), babble)
            conditions = [("complex", complicated)] + ([("clean", audio)] if index < 20 else [])
            for condition, signal in conditions:
                for channel in PROTOCOL["channels"]:
                    waveform = signal if channel == "browser16k" else g711(signal, channel.rsplit("_", 1)[1])
                    target = (
                        external_directory(root / "prepared" / split) / f"{base}-{condition}-{channel}.wav"
                    )
                    sf.write(target, waveform, 16000, subtype="PCM_16")
                    records.append(
                        {
                            "id": f"{base}-{condition}-{channel}",
                            "base_id": base,
                            "sentence_id": int(row[0]),
                            "split": split,
                            "speaker": None,
                            "gender": row[6],
                            "reference": row[2],
                            "official_normalized_reference": row[3],
                            "reference_words": len(tokens(row[2])),
                            "audio": str(target),
                            "sha256": sha256(target),
                            "source_audio": str(source),
                            "source_sha256": sha256(source),
                            "condition": condition,
                            "channel": channel,
                            "noise": noise if condition == "complex" else None,
                            "duration_s": len(waveform) / 16000,
                            "has_literal_numbers": any(char.isdecimal() for char in row[2]),
                        }
                    )
            if (index + 1) % 20 == 0:
                print(f"Prepared {split} {index + 1}/{len(rows)} base recordings", flush=True)
    metadata = {
        "protocol": PROTOCOL,
        "dataset": "google/fleurs",
        "revision": REVISION,
        "license": "CC-BY-4.0",
        "attribution": "FLEURS, Conneau et al. 2022; Google; https://arxiv.org/abs/2205.12446",
        "exclusions": exclusions,
        "excluded_sentence_ids": sorted(excluded),
        "noise_donors": [
            {"audio": r[1], "sentence_id": int(r[0]), "sha256": sha256(original / r[1])} for r in donors[:4]
        ],
        "source_metadata_sha256": {split: sha256(root / f"{split}.tsv") for split in selected},
        "source_archives_sha256": {split: sha256(root / f"{split}.tar.gz") for split in selected},
        "preparer_sha256": sha256(Path(__file__)),
        "records": records,
    }
    manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    (root / "frozen-manifest-v3.sha256").write_text(sha256(manifest) + "\n")
    for split in selected:
        subset = [
            r
            for r in records
            if r["split"] == split and r["channel"] == "browser16k" and r["condition"] == "complex"
        ]
        print(
            f"Frozen {split}: {len(subset)} human recordings, {sum(r['reference_words'] for r in subset)} words, {sum(r['duration_s'] for r in subset):.1f}s",
            flush=True,
        )
    print(f"Manifest: {manifest}; SHA256 {sha256(manifest)}", flush=True)


if __name__ == "__main__":
    main()
