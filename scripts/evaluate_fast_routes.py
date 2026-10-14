"""CPU-only frozen receptionist routing coverage; not a production traffic estimate."""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.fast_intents import (  # noqa: E402
    control_request,
    extract_common,
    mixed_request,
    requested_slots,
    requests_human,
    social_reply,
)
from app.grounding import fast_faq_reply  # noqa: E402


def route(case, suite):
    if case.get("needs_consent"):
        return {"route": "fallback"}
    context = case.get("context", {})
    if not mixed_request(case["text"]) and requests_human(case["text"]):
        return {"route": "intent", "intent": "handoff"}
    if control := control_request(case["text"], context, has_draft=case.get("has_draft", False)):
        return {"route": "control", "control": control}
    config = case.get("agent_config", {})
    if config.get("instructions") or config.get("category_schema"):
        return {"route": "fallback"}
    if reply := social_reply(case["text"], allowed_actions=config.get("allowed_actions")):
        return {"route": "social", **reply}
    now = datetime.fromisoformat(suite["now"])
    if lookup := requested_slots(case["text"], context, suite["services"], now):
        return {"route": "availability", **lookup}
    if reply := fast_faq_reply(case["text"], suite["services"], suite["knowledge"], suite["schedule"]):
        return {"route": "faq", **reply}
    intent = extract_common(case["text"], context, suite["services"], now)
    return {"route": "intent", **intent.model_dump(exclude_defaults=True)} if intent is not None else {"route": "fallback"}


def evaluate(suite, split):
    records = []
    for case in suite["cases"]:
        if split != "all" and case["split"] != split:
            continue
        start = time.perf_counter_ns()
        result = route(case, suite)
        microseconds = (time.perf_counter_ns() - start) / 1000
        expected = case["expected"]
        errors = []
        for field in ("route", "intent", "control", "service_id", "date", "time", "name", "phone", "message"):
            if field in expected and result.get(field) != expected[field]:
                errors.append(f"{field}: expected {expected[field]!r}, got {result.get(field)!r}")
        errors += [f"missing {field}" for field in expected.get("required_fields", []) if not result.get(field)]
        errors += [f"unmentioned {field}" for field in expected.get("absent", []) if result.get(field)]
        errors += [f"missing fact {term}" for term in expected.get("contains", []) if term.lower() not in result.get("text", "").lower()]
        records.append({"id": case["id"], "group": case["group"], "split": case["split"], "eligible_common": expected["route"] != "fallback", "result": result, "errors": errors, "microseconds": microseconds})
    fast = [r for r in records if r["result"]["route"] != "fallback"]
    wrong = [r for r in fast if r["errors"]]
    eligible = [r for r in records if r["eligible_common"]]
    correct_eligible = [r for r in fast if r["eligible_common"] and not r["errors"]]
    return {
        "summary": {
            "split": split, "cases": len(records), "fast_routes": len(fast), "correct_fast_routes": len(fast) - len(wrong),
            "wrong_fast_routes": len(wrong), "fallbacks": len(records) - len(fast), "all_expectations_passed": sum(not r["errors"] for r in records),
            "fast_coverage": len(fast) / len(records) if records else None,
            "safe_fast_coverage": (len(fast) - len(wrong)) / len(records) if records else None,
            "eligible_common": len(eligible), "correct_eligible_fast": len(correct_eligible),
            "eligible_common_coverage": len(correct_eligible) / len(eligible) if eligible else None,
            "by_group": dict(Counter(r["group"] for r in fast)),
            "limitations": "Synthetic frozen routing fixture; includes safety/unsupported cases in denominator. Exact critical-field correctness is tested separately. CPU decision time excludes database, model and audio. No real-world majority claim.",
        },
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "tests/fixtures/fast_receptionist_routing.json")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="dev")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.suite.read_text()), args.split)
    report["suite_sha256"] = hashlib.sha256(args.suite.read_bytes()).hexdigest()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for row in report["records"]:
        if row["errors"]:
            print(row["id"], "; ".join(row["errors"]))
    return int(any(row["errors"] for row in report["records"]))


if __name__ == "__main__":
    raise SystemExit(main())
