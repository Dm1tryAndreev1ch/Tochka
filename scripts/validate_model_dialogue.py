"""Validate real LocalModels + Dialogue + Business against synthetic scenarios.

Uses an already running loopback LLM and a disposable PostgreSQL database on
the configured database server. Does not start GPU, voice, CRM or app workers.
This is a reproducible invariant check, not a human conversation-quality score.
"""

import argparse
import asyncio
import hashlib
import json
import math
import platform
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.business import MINSK, Business  # noqa: E402
from app.config import settings  # noqa: E402
from app.dialogue import Dialogue, EventBus  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.models import LocalModels  # noqa: E402

TABLES = ("actions", "bookings", "requests", "outbox")
ZERO = {table: 0 for table in TABLES}
CONFIRMED = {"actions": 1, "bookings": 1, "requests": 1, "outbox": 3}
PUBLISHED = "Гарантия на результат диагностики действует четырнадцать календарных дней."
UNPUBLISHED = "Гарантия на результат диагностики действует пять лет."
NAME = "Алексей"
PHONE = "+375291234567"


def json_safe(value):
    if isinstance(value, dict):
        return {
            key: json_safe(item)
            for key, item in value.items()
            if key not in ("management_token", "token", "token_hash")
        }
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    return value


def percentile(values, proportion):
    return sorted(values)[max(0, math.ceil(len(values) * proportion) - 1)] if values else None


def report_path(path):
    return path if path.suffix == ".json" else path / "model-dialogue-validation.json"


def loopback_endpoint(value):
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("LLM endpoint must be an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LLM endpoint must not contain credentials or query parameters")
    if parsed.path.rstrip("/") not in ("", "/v1"):
        raise ValueError("LLM endpoint must use the root or /v1 path")
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))


def database_endpoint(name):
    # Preserve configured credentials without exposing the DSN in the report.
    parsed = urlsplit(settings.database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + name, parsed.query, ""))


class Observer:
    """Observe real requests and extraction; never supply model results."""

    def __init__(self, models):
        self.requests = []
        self.extractions = []
        self.original_extract = models.extract
        models.extract = self.extract
        models.http.event_hooks = {"request": [self.request], "response": [self.response]}

    async def request(self, request):
        row = {"method": request.method, "path": request.url.path}
        request.extensions["validation_started"] = time.perf_counter()
        request.extensions["validation_row"] = row
        self.requests.append(row)

    async def response(self, response):
        row = response.request.extensions["validation_row"]
        row.update(
            status=response.status_code,
            elapsed_seconds=time.perf_counter() - response.request.extensions["validation_started"],
        )
        if response.request.url.path.endswith("/chat/completions"):
            await response.aread()
            try:
                result = response.json()
                row.update(
                    usage=result.get("usage"),
                    timings=result.get("timings"),
                    finish_reason=result.get("choices", [{}])[0].get("finish_reason"),
                )
            except (ValueError, KeyError, IndexError):
                row["response_metadata_unavailable"] = True

    async def extract(self, text, history, context, services, knowledge, now):
        row = {
            "text": text,
            "history": history,
            "customer_memory": context.get("customer_memory", {}),
            "started_at_local": now,
        }
        self.extractions.append(row)
        start = time.perf_counter()
        try:
            result = await self.original_extract(text, history, context, services, knowledge, now)
            row["output"] = result.model_dump(exclude_none=True)
            return result
        except Exception as error:
            row["error_type"] = type(error).__name__
            raise
        finally:
            row["elapsed_seconds"] = time.perf_counter() - start


class Validation:
    def __init__(self, business, dialogue, observer, report, output):
        self.business = business
        self.pool = business.pool
        self.dialogue = dialogue
        self.observer = observer
        self.report = report
        self.output = output

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(json_safe(self.report), ensure_ascii=False, indent=2) + "\n")

    async def counts(self):
        return {table: await self.pool.fetchval(f"SELECT count(*) FROM {table}") for table in TABLES}

    async def session(self):
        return UUID((await self.business.new_session("chat", True))["id"])

    async def state(self, sid):
        session = await self.pool.fetchrow("SELECT context,memory FROM sessions WHERE id=$1", sid)
        draft = await self.business.active_draft(sid)
        return {"context": session["context"], "memory": session["memory"], "active_draft": draft}

    async def case(self, case_id, sid, text, expected, verify, *, llm=True, operation=None, counts=None):
        start = time.perf_counter()
        request_index, extract_index = len(self.observer.requests), len(self.observer.extractions)
        row = {"id": case_id, "input": text, "expected": expected, "checks": {}}
        try:
            reply = await (operation() if operation else self.dialogue.message(sid, text, uuid4()))
            row["reply"] = reply
            state = await self.state(sid)
            row["state"] = state
            extracted = self.observer.extractions[extract_index:]
            row["checks"].update(await verify(reply, state, extracted))
            row["checks"]["reply_present"] = bool(reply.get("text", "").strip())
            row["checks"]["no_degraded_reply"] = not reply.get("degraded", False)
            row["checks"]["no_thinking_leak"] = not re.search(r"</?think>", reply.get("text", ""))
            if counts is None:
                row["checks"]["no_action_claim_before_confirmation"] = "result" not in reply and not re.search(
                    r"(?:запись|заявка|оплата)\s+(?:подтверждена|сохранена|выполнена|прошла)|"
                    r"(?:записал[аи]?|соединил[аи]?|перев[её]л[аи]?|позвонил[аи]?)\s+(?:вас|вам|сотруднику)",
                    reply.get("text", "").lower(),
                )
        except Exception as error:
            # Exception messages can contain configured database credentials.
            row["error_type"] = type(error).__name__
            if isinstance(error, AppError):
                row["error_code"] = error.code
            row["checks"]["scenario_completed"] = False
        row["elapsed_seconds"] = time.perf_counter() - start
        row["requests"] = self.observer.requests[request_index:]
        row["extractions"] = self.observer.extractions[extract_index:]
        row["request_count"] = len(row["requests"])
        row["llm_request_count"] = sum(item["path"].endswith("/chat/completions") for item in row["requests"])
        row["checks"]["expected_real_llm_path"] = row["llm_request_count"] >= 1 if llm else row["llm_request_count"] == 0
        row["database_counts"] = await self.counts()
        row["checks"]["database_actions_match"] = row["database_counts"] == (ZERO if counts is None else counts)
        row["failures"] = [name for name, passed in row["checks"].items() if not passed]
        row["outcome"] = "passed" if not row["failures"] else "failed"
        self.report["records"].append(row)
        self.save()
        print(f"{case_id}: {row['elapsed_seconds']:.2f}s {row['outcome'].upper()}" + (" " + ", ".join(row["failures"]) if row["failures"] else ""), flush=True)
        return row


def extraction_output(rows):
    return rows[-1].get("output", {}) if rows else {}


async def fixtures(business):
    pool = business.pool
    await pool.execute(
        "INSERT INTO knowledge(id,title,content,source,published_revision,published_title,published_content) "
        "VALUES($1,'Гарантия — новая редакция',$2,'Синтетическая проверка',1,'Гарантия',$3)",
        uuid4(), UNPUBLISHED, PUBLISHED,
    )
    await pool.execute(
        "INSERT INTO knowledge(id,title,content,source) VALUES($1,'Неопубликованный тариф',"
        "'Оплата криптовалютой принимается, скидка девяносто процентов.','Синтетическая проверка')",
        uuid4(),
    )
    services = await business.services()
    service = next(item for item in services if item["id"] == "diagnostics")
    for offset in range(1, 29):
        day = datetime.now(MINSK).date() + timedelta(days=offset)
        slots = await business.slots(service["id"], day)
        if len(slots) >= 2:
            return service, day.strftime("%d.%m.%Y"), slots[0], slots[1]
    raise RuntimeError("No future synthetic booking slots in migrated schedule")


async def scenarios(run):
    service, date_text, slot, corrected_slot = await fixtures(run.business)
    run.report["fixture"] = {
        "service": service,
        "published_content": PUBLISHED,
        "unpublished_content": UNPUBLISHED,
        "date": date_text,
        "initial_slot": slot,
        "corrected_slot": corrected_slot,
        "customer_name": NAME,
        "synthetic_phone": PHONE,
    }
    conversation, other, booking = await run.session(), await run.session(), await run.session()

    async def faq(reply, state, extracts):
        raw = extraction_output(extracts)
        quotes = raw.get("fact_quotes", [])
        return {
            "published_quote_returned": reply.get("response_source") == "knowledge" and "четырнадцать календарных дней" in reply["text"],
            "model_quotes_contiguous_published_source": bool(quotes) and all(quote and quote in PUBLISHED for quote in quotes),
            "unpublished_revision_absent": "пять лет" not in reply["text"].lower(),
        }

    await run.case("published_faq", conversation, "Как долго действует гарантия на результат диагностики?", {"response_source": "knowledge", "published_fact": PUBLISHED}, faq)

    async def missing(reply, state, extracts):
        return {
            "honest_missing_fact": reply.get("response_source") == "missing_fact",
            "no_unpublished_claim": "девяносто" not in reply["text"].lower() and "принимается" not in reply["text"].lower(),
        }

    await run.case("missing_company_fact", conversation, "Принимает ли компания оплату криптовалютой?", {"response_source": "missing_fact", "unpublished_document_unavailable": True}, missing)

    async def substantive(reply, state, extracts):
        raw = extraction_output(extracts)
        return {
            "conversation_intent": raw.get("intent") == "conversation",
            "substantive_topic_heuristic": len(reply["text"].strip()) >= 40 and bool(re.search(r"отдых|вечер|прогул|книг|экран|расслаб|сон|чай|музык", reply["text"], re.I)),
            "name_saved_in_own_session": state["memory"].get("customer_name") == NAME,
            "memory_passed_to_real_model": bool(extracts) and extracts[-1]["customer_memory"].get("customer_name") == NAME,
        }

    introduction = "Меня зовут Алексей. Сегодня весь день работал без перерыва и устал. Помоги придумать спокойный вечер без экранов."
    await run.case("substantive_conversation", conversation, introduction, {"intent": "conversation", "topic": "спокойный вечер без экранов", "minimum_reply_characters": 40}, substantive)

    async def history(reply, state, extracts):
        raw = extraction_output(extracts)
        previous = extracts[-1]["history"] if extracts else []
        return {
            "conversation_intent": raw.get("intent") == "conversation",
            "prior_user_and_reply_sent_to_model": any(item["role"] == "user" and item["text"] == introduction for item in previous) and any(item["role"] == "assistant" for item in previous),
            "substantive_history_topic_heuristic": len(reply["text"].strip()) >= 40 and bool(re.search(r"экран|прогул|вечер|дожд|книг|дома|домаш|музык|чай|отдых", reply["text"], re.I)),
        }

    await run.case("conversation_history", conversation, "Вернёмся к твоему совету про мой вечер без экранов. Если пойдёт дождь и прогулка сорвётся, чем заменить её дома?", {"intent": "conversation", "history_contains_introduction": True, "topic": "вечер дома без экранов"}, history)

    async def recall(reply, state, extracts):
        return {"known_name_recalled": NAME in reply["text"], "uses_session_memory": reply.get("response_source") == "session_memory"}

    await run.case("name_recall", conversation, "Как меня зовут?", {"name": NAME, "response_source": "session_memory", "llm_requests": 0}, recall, llm=False)

    async def isolation(reply, state, extracts):
        return {"other_name_absent": NAME not in reply["text"], "other_memory_empty": state["memory"] == {}, "uses_session_memory": reply.get("response_source") == "session_memory"}

    await run.case("name_isolation", other, "Как меня зовут?", {"other_session_name_unknown": True, "llm_requests": 0}, isolation, llm=False)

    async def vague(reply, state, extracts):
        raw = extraction_output(extracts)
        return {
            "booking_intent": raw.get("intent") == "booking" and state["context"].get("intent") == "booking",
            "correct_service": raw.get("service_id") == service["id"],
            "critical_fields_not_invented": not any(raw.get(field) or state["context"].get(field) for field in ("phone", "date", "time")),
            "incomplete_booking_has_no_draft": state["active_draft"] is None,
        }

    await run.case("vague_semantic_booking", booking, f"Хотел бы прийти на {service['name']} в один из ближайших дней после обеда; меня зовут Алексей. Точную дату и часы ещё не выбрал.", {"intent": "booking", "service_id": service["id"], "absent": ["date", "time", "phone"], "draft": None}, vague)

    async def no_country(reply, state, extracts):
        raw = extraction_output(extracts)
        return {"country_prefix_not_invented": not raw.get("phone") and not state["context"].get("phone"), "no_incomplete_phone_draft": state["active_draft"] is None}

    await run.case("phone_without_country", booking, "Мой номер — двадцать девять, сто двадцать три, сорок пять, шестьдесят семь; код страны я ещё не называл.", {"phone": None, "country_code_not_guessed": True, "draft": None}, no_country)

    async def complete(reply, state, extracts):
        raw = extraction_output(extracts)
        draft = state["active_draft"] or {}
        payload = draft.get("payload", {})
        return {
            "booking_intent": raw.get("intent") == "booking",
            "draft_requires_confirmation": draft.get("status") == "awaiting_confirmation" and "подтверждаете" in reply["text"].lower(),
            "explicit_fields_preserved": payload.get("service_id") == service["id"] and payload.get("name") == NAME and payload.get("phone") == PHONE and payload.get("starts_at") == slot["starts_at"],
            "no_confirmation_result": "result" not in reply,
        }

    await run.case("complete_booking_unconfirmed", booking, f"Хочу оформить визит на {service['name']} {date_text} в {slot['label']}, меня зовут Алексей, мой телефон {PHONE}. Пока не хочу подтверждать: сначала покажите сводку.", {"intent": "booking", "starts_at": slot["starts_at"], "draft_status": "awaiting_confirmation", "actions": ZERO}, complete)
    previous_draft = await run.business.active_draft(booking)

    async def correction(reply, state, extracts):
        raw = extraction_output(extracts)
        current = state["active_draft"] or {}
        payload = current.get("payload", {})
        old_status = await run.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(previous_draft["id"])) if previous_draft else None
        stale_rejected = False
        if previous_draft:
            try:
                await run.business.confirm(booking, UUID(previous_draft["id"]), previous_draft["revision"], uuid4())
            except AppError as error:
                stale_rejected = error.code == "stale_confirmation"
        return {
            "new_time_extracted": raw.get("intent") == "booking" and raw.get("time") == corrected_slot["label"],
            "unmentioned_fields_not_copied_by_model": not any(raw.get(field) for field in ("name", "phone", "date", "service_id")),
            "corrected_draft_preserves_server_context": payload.get("starts_at") == corrected_slot["starts_at"] and payload.get("name") == NAME and payload.get("phone") == PHONE and payload.get("service_id") == service["id"],
            "previous_draft_superseded": bool(previous_draft) and current.get("id") != previous_draft["id"] and old_status == "superseded",
            "stale_confirmation_rejected": stale_rejected,
        }

    await run.case("booking_correction", booking, f"Перенесите начало на {corrected_slot['label']}, это ровно на пятнадцать минут позже. Остальные данные те же, пока без подтверждения.", {"time": corrected_slot["label"], "other_fields_not_extracted": True, "previous_draft_status": "superseded", "stale_confirmation": "rejected"}, correction)

    async def thinking(reply, state, extracts):
        return {"no_action_result": "result" not in reply, "awaiting_explicit_confirmation": bool(state["active_draft"]) and state["active_draft"]["status"] == "awaiting_confirmation"}

    await run.case("acknowledgement_is_not_confirmation", booking, "Сводка понятна, мне нужно подумать.", {"actions": ZERO, "draft_status": "awaiting_confirmation"}, thinking)
    final_draft = await run.business.active_draft(booking)
    confirmation_key = uuid4()

    async def confirm_and_repeat():
        reply = await run.dialogue.message(booking, "Да, подтверждаю", confirmation_key)
        before = await run.counts()
        repeated = await run.dialogue.message(booking, "Да, подтверждаю", confirmation_key)
        after = await run.counts()
        retry = None
        if final_draft:
            retry = await run.business.confirm(booking, UUID(final_draft["id"]), final_draft["revision"], uuid4())
        reply["validation_retries"] = {"same_message_reply": repeated, "new_business_key_result": retry, "before": before, "after": after, "after_business_retry": await run.counts()}
        return reply

    async def confirmation(reply, state, extracts):
        result, retries = reply.get("result", {}), reply.get("validation_retries", {})
        same = retries.get("same_message_reply", {}).get("result", {})
        retry = retries.get("new_business_key_result") or {}
        stored = await run.pool.fetchrow("SELECT service_id,starts_at,name,phone FROM bookings LIMIT 1")
        outbox = await run.pool.fetch("SELECT status,attempts,external_id FROM outbox")
        return {
            "confirmation_succeeded": result.get("status") == "succeeded",
            "one_action_after_every_retry": all(retries.get(field) == CONFIRMED for field in ("before", "after", "after_business_retry")),
            "same_booking_id_returned": bool(result.get("booking_id")) and result.get("booking_id") == same.get("booking_id") == retry.get("booking_id"),
            "confirmed_corrected_details": bool(stored) and stored["service_id"] == service["id"] and stored["starts_at"].astimezone(MINSK).isoformat() == corrected_slot["starts_at"] and stored["name"] == NAME and stored["phone"] == PHONE,
            "no_crm_delivery_attempted": len(outbox) == 3 and all(item["status"] == "pending" and item["attempts"] == 0 and item["external_id"] is None for item in outbox),
        }

    await run.case("explicit_confirmation_and_idempotency", booking, "Да, подтверждаю", {"actions": CONFIRMED, "same_booking_on_retries": True, "crm_delivery_attempts": 0, "llm_requests": 0}, confirmation, llm=False, operation=confirm_and_repeat, counts=CONFIRMED)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm-url", required=True, help="Existing HTTP loopback llama-server root or /v1 URL")
    parser.add_argument("--model", required=True, help="Existing server's advertised model alias")
    parser.add_argument("--profile", choices=("legacy", "compact"), default="compact")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/latency-20261013/model-dialogue-validation.json", help="JSON report path, or directory")
    args = parser.parse_args()
    output = report_path(args.output)
    llm_url = loopback_endpoint(args.llm_url)
    report = {
        "metadata": {
            "platform": platform.platform(), "python": sys.version.split()[0],
            "model": args.model, "llm_url": llm_url, "prompt_profile": args.profile,
            "started_at": datetime.now(MINSK).isoformat(), "expected_scenarios": 12,
            "llm_context_tokens": settings.llm_context_tokens, "llm_reply_tokens": settings.llm_reply_tokens,
            "real_components": ["LocalModels.extract", "Dialogue.message", "Business"],
            "workers_started": [], "harness_warmup_requests": 0,
            "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT / "app/models.py", ROOT / "app/dialogue.py", ROOT / "app/business.py", ROOT / "app/grounding.py", ROOT / "app/llm_prompt.py", ROOT / "app/schemas.py")},
            "limitations": "Synthetic chat dialogue and database invariants only. Topic checks are declared string heuristics, not human conversation ratings. No STT, TTS, audio or speaker validation. External server state/warmup is not controlled. Only loopback LLM HTTP requests and PostgreSQL are used; pending synthetic CRM outbox rows are never delivered.",
        },
        "records": [], "validity": {"temporary_database_created": False, "temporary_database_dropped": False},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    settings.llm_url, settings.llm_model = llm_url, args.model
    settings.llm_prompt_profile, settings.llm_thinking = args.profile, False
    database = "voice_dialogue_validation_" + uuid4().hex[:16]
    admin, pool, models, run = None, None, None, None
    private_runtime = tempfile.TemporaryDirectory(prefix="voice-dialogue-validation-")
    previous_runtime = settings.private_runtime_dir
    settings.private_runtime_dir = private_runtime.name
    try:
        headers = {"Authorization": "Bearer " + settings.llm_api_key} if settings.llm_api_key else {}
        async with httpx.AsyncClient(trust_env=False, timeout=3, headers=headers) as client:
            available = await client.get(llm_url + "/models")
            available.raise_for_status()
            if not any(item.get("id") == args.model for item in available.json().get("data", [])):
                raise RuntimeError("Requested model alias not advertised by existing server")
        admin = await asyncpg.connect(settings.database_url)
        await admin.execute(f'CREATE DATABASE "{database}"')
        report["validity"]["temporary_database_created"] = True
        pool = await db.connect(database_endpoint(database))
        models = LocalModels()
        observer = Observer(models)
        business = Business(pool)
        run = Validation(business, Dialogue(business, models, EventBus()), observer, report, output)
        await scenarios(run)
    except Exception as error:
        report["fatal_error_type"] = type(error).__name__
        print(f"Validation stopped: {type(error).__name__}", flush=True)
    finally:
        if models:
            await models.close()
        if pool:
            await pool.close()
        if admin:
            try:
                if report["validity"]["temporary_database_created"]:
                    await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
                    report["validity"]["temporary_database_dropped"] = True
            finally:
                await admin.close()
        settings.private_runtime_dir = previous_runtime
        private_runtime.cleanup()
        rows = report["records"]
        report["validity"]["all_scenarios_recorded"] = len(rows) == 12
        report["validity"]["real_llm_requests_observed"] = sum(row["llm_request_count"] for row in rows)
        report["summary"] = {
            "scenarios": len(rows), "passed": sum(row["outcome"] == "passed" for row in rows),
            "failed": sum(row["outcome"] == "failed" for row in rows),
            "failed_scenarios": [row["id"] for row in rows if row["outcome"] == "failed"],
            "llm_requests": sum(row["llm_request_count"] for row in rows),
            "llm_http_requests_including_tokenization": sum(row["request_count"] for row in rows),
            "dialogue_elapsed_p50_seconds": percentile([row["elapsed_seconds"] for row in rows], .5),
            "dialogue_elapsed_p95_seconds": percentile([row["elapsed_seconds"] for row in rows], .95),
            "completed_at": datetime.now(MINSK).isoformat(),
        }
        output.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    valid = all(report["validity"].get(key) for key in ("temporary_database_created", "temporary_database_dropped", "all_scenarios_recorded"))
    return 0 if valid and not report.get("fatal_error_type") and report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
