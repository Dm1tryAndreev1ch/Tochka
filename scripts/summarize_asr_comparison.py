"""Read immutable ASR results, rank complete dev runs and retain an auditable error drilldown."""

import argparse
import json
from collections import Counter
from pathlib import Path

from download_stt_candidates import EXTERNAL, external_directory, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=EXTERNAL / "asr-20261013")
    args = parser.parse_args()
    directory = external_directory(args.directory)
    summaries = []
    for split in ["dev", "test"]:
        for path in sorted((directory / split).glob("*-*.json")):
            report = json.loads(path.read_text())
            complex_groups = {
                key: value for key, value in report.get("summary", {}).items() if key.endswith("/complex")
            }
            item = {
                "model": report["model"],
                "split": split,
                "complete": report["complete"],
                "source_report": str(path),
                "source_report_sha256": sha256(path),
                "manifest_sha256": report["manifest_sha256"],
                "source_hashes": report["source_hashes"],
                "requested_records": report["requested_records"],
                "measured_records": len(report["results"]),
                "channels": complex_groups,
                "worst_channel_wer": max(
                    (g["normalized"]["wer"] for g in complex_groups.values()), default=None
                ),
                "worst_channel_p95_s": max(
                    (g["latency_p95_s"] for g in complex_groups.values()), default=None
                ),
                "all_channels_under_wer_6_percent": report["complete"]
                and len(complex_groups) == 3
                and all(g["normalized"]["meets_wer_6_percent"] for g in complex_groups.values()),
            }
            errors = [
                row
                for row in report["results"]
                if row["condition"] == "complex" and row["normalized"]["errors"]
            ]
            substitutions = Counter(
                (ref, hyp)
                for row in errors
                for kind, ref, hyp in row["normalized"]["edits"]
                if kind == "substitution"
            )
            deletions = Counter(
                ref for row in errors for kind, ref, _ in row["normalized"]["edits"] if kind == "deletion"
            )
            item["drilldown"] = {
                "top_substitutions": [
                    {"reference": ref, "hypothesis": hyp, "count": count}
                    for (ref, hyp), count in substitutions.most_common(20)
                ],
                "top_deletions": deletions.most_common(20),
                "budget_exhausted": sum(bool(r["details"].get("budget_exhausted")) for r in errors),
                "empty_outputs": sum(not r["hypothesis"] for r in errors),
                "rows_with_trailing_deletions": sum(
                    r["normalized"]["edits"][-1][0] == "deletion" for r in errors
                ),
                "note": "Trailing deletions indicate omitted tails, not proof of VAD truncation; whole frozen audio was supplied without VAD. Numeric-token differences may include correct word-number formatting; raw primary counts are retained.",
                "worst_examples": [
                    {
                        "id": r["id"],
                        "reference": r["reference"],
                        "hypothesis": r["hypothesis"],
                        "words": r["normalized"]["reference_words"],
                        "errors": r["normalized"]["errors"],
                        "edits": r["normalized"]["edits"],
                        "error": r["error"],
                    }
                    for r in sorted(errors, key=lambda row: row["normalized"]["errors"], reverse=True)[:10]
                ],
            }
            summaries.append(item)
    dev = [r for r in summaries if r["split"] == "dev" and r["complete"]]
    ordered = sorted(dev, key=lambda row: (row["worst_channel_wer"], row["worst_channel_p95_s"]))
    new = [r for r in ordered if r["model"] != "whisper-turbo-q5"]
    qualifying = sorted(
        (r for r in new if r["all_channels_under_wer_6_percent"]), key=lambda row: row["worst_channel_p95_s"]
    )
    selected = ["whisper-turbo-q5"]
    if new:
        selected.append(new[0]["model"])
        second = qualifying[0]["model"] if qualifying else (new[1]["model"] if len(new) > 1 else None)
        if second and second not in selected:
            selected.append(second)
    result = {
        "scope": "frozen public human read-speech with controlled noise/echo/G.711, not physical microphone/SIP network acceptance or receptionist NLU accuracy",
        "selection_rule": str(directory / "selection-rule.json"),
        "protocol_annotations": str(directory / "protocol-annotations.json"),
        "dev_ranking": [r["model"] for r in ordered],
        "provisional_test_candidates": selected,
        "dev_complete_models": len(dev),
        "models": summaries,
    }
    output = directory / "comparison-summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for row in ordered:
        print(
            row["model"],
            f"worst WER {100 * row['worst_channel_wer']:.3f}%",
            f"worst p95 {row['worst_channel_p95_s']:.3f}s",
            f"all_channels_pass={row['all_channels_under_wer_6_percent']}",
        )
    print("Provisional test candidates:", ", ".join(selected))
    print("Summary:", output)


if __name__ == "__main__":
    main()
