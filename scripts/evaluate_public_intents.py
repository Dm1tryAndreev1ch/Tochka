"""Intent diagnostics on actual noisy-audio ASR results, not reference text."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.models import LocalModels  # noqa: E402

CATEGORIES = {
    "abroad": "Использование банковских услуг или карты за границей",
    "address": "Изменение почтового адреса или адреса клиента",
    "app_error": "Ошибка или неисправность банковского приложения",
    "atm_limit": "Лимит снятия наличных в банкомате",
    "balance": "Узнать баланс или остаток денег на счете",
    "business_loan": "Кредит для бизнеса или компании",
    "card_issues": "Карта не работает или оплата картой не получается",
    "cash_deposit": "Внести наличные деньги на счет",
    "direct_debit": "Прямой дебет, автоматические регулярные списания",
    "freeze": "Заблокировать или заморозить карту",
    "high_value_payment": "Крупный платеж или ограничение суммы платежа",
    "joint_account": "Совместный банковский счет",
    "latest_transactions": "История или последние операции",
    "pay_bill": "Оплатить счет или квитанцию",
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", type=Path, default=ROOT / "artifacts/asr-public-dev.json")
    parser.add_argument(
        "--manifest", type=Path, default=Path.home() / ".cache/voice-desk/evaluation/minds14-ru/manifest.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/nlu-public-dev.json")
    parser.add_argument("--profiles", nargs="+", default=["phone", "combined"])
    args = parser.parse_args()
    asr = json.loads(args.asr.read_text())
    corpus = json.loads(args.manifest.read_text())
    labels = {r["id"]: r["intent"] for r in corpus["records"]}
    models = LocalModels()
    results = []
    report = {
        "scope": "public banking category diagnostic via actual audio->ASR->NLU; not receptionist action acceptance",
        "asr_source": str(args.asr),
        "dataset": corpus["dataset"],
        "source": corpus["source"],
        "revision": corpus["revision"],
        "split": asr["split"],
        "taxonomy": CATEGORIES,
        "results": results,
    }
    try:
        for row in asr["results"]:
            if row["profile"] not in args.profiles:
                continue
            try:
                result = await models.extract(
                    row["hypothesis"],
                    [],
                    {
                        "assistant_name": "Геннадий",
                        "company_name": "Банковский контрольный набор",
                        "_category_schema": CATEGORIES,
                        "_classification_only": True,
                    },
                    [],
                    [],
                    "2026-10-13",
                )
                predicted = result.category
                failure = None
            except Exception as exc:
                predicted = None
                failure = type(exc).__name__
            results.append(
                {
                    "id": row["id"],
                    "profile": row["profile"],
                    "expected": labels[row["id"]],
                    "predicted": predicted,
                    "correct": predicted == labels[row["id"]],
                    "failure": failure,
                }
            )
            report["aggregate"] = {
                p: {
                    "total": sum(r["profile"] == p for r in results),
                    "correct": sum(r["correct"] and r["profile"] == p for r in results),
                }
                for p in args.profiles
            }
            for item in report["aggregate"].values():
                item["accuracy"] = 100 * item["correct"] / item["total"] if item["total"] else None
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(len(results), row["profile"], "correct", results[-1]["correct"], flush=True)
    finally:
        await models.close()
    print(json.dumps(report["aggregate"]))


if __name__ == "__main__":
    asyncio.run(main())
