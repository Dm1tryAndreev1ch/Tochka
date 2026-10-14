"""Fast decisions must preserve evidence, current state and transaction guards."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.business import MINSK
from app.dialogue import Dialogue, EventBus
from app.errors import AppError
from app.fast_intents import (
    extract_common,
    phone_correction_requested,
    requested_slots,
    simple_calendar_date,
    social_reply,
    spoken_digit_phone,
    unresolved_phone_correction,
)
from app.grounding import fast_faq_reply
from app.schemas import CallbackDraft, ExtractedIntent

SUITE = json.loads((Path(__file__).parent / "fixtures/fast_receptionist_routing.json").read_text())
SERVICES = SUITE["services"]
NOW = datetime.fromisoformat(SUITE["now"])
CALENDAR = json.loads((Path(__file__).parent / "fixtures/fast_calendar_routing.json").read_text())
BOOKING = {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00", "name": "Анна"}


class CountingModel:
    def __init__(self):
        self.calls = []

    async def extract(self, *args):
        self.calls.append(args)
        return ExtractedIntent(intent="conversation", answer="Уточните, пожалуйста, вопрос.")


@pytest.mark.parametrize("case", CALENDAR["cases"], ids=lambda case: case["id"])
def test_frozen_explicit_calendar_cases(case):
    now = datetime.fromisoformat(case["now"])
    horizon = case.get("booking_horizon_days")
    assert simple_calendar_date(case["text"], now, booking_horizon_days=horizon) == case["expected_date"]
    parsed = extract_common(case["text"], {"intent": "booking", "service_id": "consultation"}, SERVICES, now, booking_horizon_days=horizon)
    if case["expected_date"]:
        assert parsed.date == case["expected_date"]
        assert parsed.name is None
    else:
        assert parsed is None


@pytest.mark.parametrize("text", ["В первую пятницу ноября", "В последнюю пятницу", "В пятницу в ноябре", "В прошедшую пятницу"])
def test_weekday_qualifiers_do_not_collapse_to_this_week(text):
    assert simple_calendar_date(text, NOW) is None
    assert extract_common(text, BOOKING, SERVICES, NOW) is None


@pytest.mark.parametrize("text", [
    "плюс три семь пять два девять ноль ноль ноль один два три четыре",
    "Мой телефон: плюс три семь пять, два девять, ноль ноль ноль, один два три четыре.",
    "Номер для связи +375 (29) 000 12 34",
])
def test_spelled_phone_has_exact_explicit_country_and_digits(text):
    assert spoken_digit_phone(text) == "+375290001234"
    intent = extract_common(text, BOOKING, SERVICES, NOW)
    assert intent.phone == "+375290001234"
    assert intent.name is intent.time is intent.date is None


@pytest.mark.parametrize("text", [
    "три семь пять два девять ноль ноль ноль один два три четыре",
    "плюс триста семьдесят пять двадцать девять ноль ноль ноль двенадцать тридцать четыре",
    "плюс ноль семь пять два девять ноль ноль ноль один два три четыре",
    "плюс 375290001234 abc", "плюс три семь пять 29_0001234", "плюс 375290001234?",
    "плюс три семь пять", "плюс 37529000123456789", "плюс 375290001234 добавочный 12",
    "плюс 375290001234 или 375290009999", "плюс 375290001234, меня зовут Иван",
])
def test_spelled_phone_never_guesses_or_drops_tokens(text):
    assert spoken_digit_phone(text) is None


@pytest.mark.parametrize("text", [
    "После 10:00.", "Завтра и послезавтра.", "+375290001234 и +375290009999",
    "Примерно в десять.", "Сначала завтра, потом 16.10.2026.",
    "В 09:30:15.", "+375290001234abc", "+375290001234 добавочный 12",
])
def test_critical_fields_with_constraints_or_alternatives_fall_through(text):
    assert extract_common(text, BOOKING, SERVICES, NOW) is None


@pytest.mark.parametrize("text", ["Есть свободное время на консультацию завтра?", "Свободные окна на диагностику 16.10.2026?"])
def test_slots_lookup_is_only_an_explicit_service_and_date(text):
    result = requested_slots(text, {}, SERVICES, NOW)
    assert result in ({"service_id": "consultation", "date": "2026-10-14"}, {"service_id": "diagnostics", "date": "2026-10-16"})


@pytest.mark.parametrize("text", [
    "Свободные окна на консультацию 16 октября 2026?",
    "Свободное время на консультацию 2026-10-16?",
    "Свободные окна на консультацию в эту пятницу?",
])
def test_availability_uses_the_same_explicit_calendar(text):
    assert requested_slots(text, {}, SERVICES, NOW) == {"service_id": "consultation", "date": "2026-10-16"}


@pytest.mark.parametrize("text", [
    "Есть свободное время завтра?", "Есть свободное время на консультацию?",
    "Свободные окна на консультацию завтра после 12:00?",
    "Свободные окна на консультацию завтра или послезавтра?",
    "Запишите на консультацию завтра на свободное время.",
    "Свободное время для диагностики и консультации завтра?",
    "Свободное время на консультацию завтра в первой половине дня?",
    "Свободное время на консультацию завтра у Анны?",
])
def test_incomplete_slots_or_action_requests_fall_through(text):
    assert requested_slots(text, {}, SERVICES, NOW) is None


@pytest.mark.parametrize("text", [
    "Сколько стоит ремонт космического корабля?", "Адрес и скидки студентам?",
    "Какой адрес сайта?", "Можно оплатить переводом?", "Какая гарантия на консультацию?",
    "Где вы находитесь и сколько стоит консультация?", "Какие часы работы в праздники?",
    "Расскажите о гарантии и запишите на настройку.",
    "Исправьте мой адрес на другой.", "Как поменять адрес в заявке?", "Я уже оплатил картой.",
    "Моя почта user@example.test.", "Карта не работает.", "Оплата не прошла.",
    "Какой статус платежа?", "Адрес неверный.", "Сколько стоит срочная диагностика?",
    "Сколько стоит консультация на дому?", "Сколько стоит консультация для ребёнка?",
    "Какие услуги для детей?", "Как долго длится срочная диагностика?",
    "Запишите меня на консультацию. Какие услуги у вас есть?",
    "Хочу записаться на консультацию, покажите прайс.",
])
def test_unknown_qualifiers_and_multiple_requests_do_not_get_partial_faq(text):
    assert fast_faq_reply(text, SERVICES, SUITE["knowledge"], SUITE["schedule"]) is None


@pytest.mark.parametrize("text", ["Плюс три семь", "Без имени", "Пропустим", "Пропустите", "Отказываюсь"])
def test_name_prompt_rejects_phone_prefix_and_refusal(text):
    context = {k: v for k, v in BOOKING.items() if k != "name"}
    assert extract_common(text, context, SERVICES, NOW) is None


@pytest.mark.parametrize("text", [
    "Запишите меня на консультацию завтра в 10, а телефон исправьте на 80291234567",
    "Запишите меня на консультацию завтра в 10, а имя исправьте на 123",
    "Запишите меня на консультацию. Какие услуги у вас есть?",
    "Хочу записаться на консультацию, покажите прайс.",
])
def test_unparsed_correction_or_second_request_cannot_reuse_old_fields(text):
    assert extract_common(text, BOOKING, SERVICES, NOW) is None


@pytest.mark.parametrize("text", [
    "Не меняйте телефон", "Телефон не меняйте", "Не нужно менять номер", "Оставьте прежний номер",
    "Оставьте мой телефон", "Какой мой телефон?",
    "Не нужно телефон исправлять", "Не надо номер изменять", "Телефон исправлять не нужно",
    "Какой теперь мой номер?",
])
def test_keep_phone_is_not_a_correction(text):
    context = {**BOOKING, "phone": "+375290001234"}
    assert not phone_correction_requested(text, context)
    assert not unresolved_phone_correction(text, context, None)


@pytest.mark.parametrize("text,extracted_phone,unresolved", [
    ("Телефон исправьте на 80291234567", None, True),
    ("Телефон исправьте на 80291234567", "+375290001234", True),
    ("Новый номер +375290009999", None, True),
    ("Номер не +375290001234, а +375290009999", "+375290001234", True),
    ("Измените телефон с +375290001234 на +375290009999", "+375290001234", True),
    ("Измените телефон с +375290001234 на +375290009999", "+375290009999", False),
    ("Телефон исправьте на +375290001234", "+375290001234", False),
    ("Не +375290001234, а +375290009999", "+375290001234", True),
    ("Вместо +375290001234 укажите +375290009999", None, True),
])
def test_phone_correction_requires_resolved_new_value(text, extracted_phone, unresolved):
    assert unresolved_phone_correction(text, {**BOOKING, "phone": "+375290001234"}, extracted_phone) is unresolved


def test_capabilities_do_not_advertise_disabled_actions():
    answer = social_reply("Что ты умеешь?", allowed_actions=[])["text"]
    assert "опубликованных" in answer
    assert all(term not in answer for term in ("записью", "звонок", "подключение"))


async def publish(business, content, *, published=None):
    kid = uuid4()
    await business.pool.execute(
        "INSERT INTO knowledge(id,title,content,source,published_revision,published_title,published_content) "
        "VALUES($1,'Тест',$2,'test',1,'Тест',$3)", kid, content, published if published is not None else content,
    )
    return kid


async def test_faq_reads_published_revision_and_current_updates_without_model(business, session, database_admin):
    sid, model = session["uuid"], CountingModel()
    # The runtime role has no DELETE on knowledge by design; clean up as the DB owner.
    await database_admin.execute("DELETE FROM knowledge")
    kid = await publish(business, "Адрес: неопубликованный секрет.", published="Адрес: улица Первая, 10.")
    dialogue = Dialogue(business, model, EventBus())
    assert await dialogue.prepare(sid, "Какой адрес компании?") is None
    reply = await dialogue.message(sid, "Какой адрес компании?", uuid4())
    assert reply["text"] == "Адрес: улица Первая, 10."
    await business.pool.execute("UPDATE knowledge SET published_content='Адрес: улица Вторая, 20.',published_revision=2 WHERE id=$1", kid)
    assert (await dialogue.message(sid, "Какой адрес компании?", uuid4()))["text"] == "Адрес: улица Вторая, 20."
    await business.pool.execute("UPDATE knowledge SET archived=true WHERE id=$1", kid)
    reply = await dialogue.message(sid, "Какой адрес компании?", uuid4())
    assert reply["response_source"] == "missing_fact"
    assert "секрет" not in reply["text"] and "Вторая" not in reply["text"]
    assert model.calls == []


async def test_source_scope_price_and_schedule_are_current(business, session, database_admin):
    sid, model = session["uuid"], CountingModel()
    # The runtime role has no DELETE on knowledge by design; clean up as the DB owner.
    await database_admin.execute("DELETE FROM knowledge")
    allowed = await publish(business, "Телефон: +375290001234.")
    await publish(business, "Адрес: закрытый источник.")
    await business.pool.execute("UPDATE sessions SET agent_config=$2 WHERE id=$1", sid, {"knowledge_ids": [str(allowed)]})
    dialogue = Dialogue(business, model, EventBus())
    assert (await dialogue.message(sid, "Ваш адрес?", uuid4()))["response_source"] == "missing_fact"
    await business.pool.execute("UPDATE services SET price=123,revision=revision+1 WHERE id='consultation'")
    assert "123" in (await dialogue.message(sid, "Сколько стоит консультация?", uuid4()))["text"]
    await business.pool.execute("UPDATE settings SET value=$1 WHERE key='schedule'", {"opens": 10, "closes": 16, "weekdays": [5], "closed_dates": []})
    answer = (await dialogue.message(sid, "Можно в субботу?", uuid4()))["text"]
    assert "10:00" in answer and "16:00" in answer and "В воскресенье не работаем" in answer
    assert model.calls == []


@pytest.mark.parametrize("config", [{"instructions": "Сначала уточните отдел."}, {"category_schema": {"required": ["topic"]}}])
@pytest.mark.parametrize("text", ["Какие услуги?", "Здравствуйте", "Запишите на консультацию"])
async def test_custom_semantics_use_model_instead_of_new_shortcuts(business, session, config, text):
    sid, model = session["uuid"], CountingModel()
    await business.pool.execute("UPDATE sessions SET agent_config=$2 WHERE id=$1", sid, config)
    dialogue = Dialogue(business, model, EventBus())
    prepared = await dialogue.prepare(sid, text)
    await dialogue.message(sid, text, uuid4(), prepared=prepared)
    assert len(model.calls) == 1
    assert model.calls[0][2]["agent_instructions"] == config.get("instructions", "")
    assert model.calls[0][2]["_category_schema"] == config.get("category_schema", {})
    assert await business.pool.fetchval("SELECT count(*) FROM drafts") == 0


async def test_new_shortcuts_preserve_allowed_actions_and_consent(business, session):
    sid, model = session["uuid"], CountingModel()
    await business.pool.execute("UPDATE sessions SET agent_config=$2 WHERE id=$1", sid, {"allowed_actions": []})
    dialogue = Dialogue(business, model, EventBus())
    for text in ("Запишите на консультацию завтра в десять", "Перезвоните", "Соедините с сотрудником"):
        assert "выключен" in (await dialogue.message(sid, text, uuid4()))["text"]
    assert await business.pool.fetchval("SELECT count(*) FROM drafts") == 0
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 0
    assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == {}
    await business.pool.execute("UPDATE sessions SET consent=false WHERE id=$1", sid)
    with pytest.raises(AppError):
        await dialogue.message(sid, "Какие услуги?", uuid4())
    assert model.calls == []


async def test_callback_collection_prepare_and_final_confirmation(business, session):
    sid, model = session["uuid"], CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    for text in ("Перезвоните", "Вопрос: срок ремонта ноутбука.", "анна", "плюс три семь пять два девять ноль ноль ноль один два три четыре"):
        before = await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid)
        assert await dialogue.prepare(sid, text) is None
        assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == before
        reply = await dialogue.message(sid, text, uuid4())
    draft = reply["draft"]
    assert draft["status"] == "awaiting_confirmation"
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 0
    assert await dialogue.prepare(sid, "Подтверждаю") is None
    key = uuid4()
    reply = await dialogue.message(sid, "Подтверждаю", key)
    assert reply["result"]["status"] == "succeeded"
    await dialogue.message(sid, "Подтверждаю", key)
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 1
    assert await business.pool.fetchval("SELECT count(*) FROM requests") == 1
    assert model.calls == []


async def test_cancel_rechecks_consent_inside_transaction(business, session, monkeypatch):
    sid, model = session["uuid"], CountingModel()
    draft = await business.draft(sid, "callback", CallbackDraft(name="Анна", phone="+375290001234", message="Тест"))
    original = business.active_draft

    async def revoke_after_draft_read(sid):
        result = await original(sid)
        await business.pool.execute("UPDATE sessions SET consent=false WHERE id=$1", sid)
        return result

    monkeypatch.setattr(business, "active_draft", revoke_after_draft_read)
    with pytest.raises(AppError):
        await Dialogue(business, model, EventBus()).message(sid, "Отмените черновик", uuid4())
    assert await business.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(draft["id"])) == "awaiting_confirmation"
    assert model.calls == []


async def test_slots_fetch_current_availability_only_at_final_message(business, session, monkeypatch):
    sid, model, calls = session["uuid"], CountingModel(), []
    date = (datetime.now(MINSK).date() + timedelta(days=1)).isoformat()
    slots = [{"label": "13:00"}]

    async def availability(service_id, day):
        calls.append((service_id, day.isoformat()))
        return list(slots)

    monkeypatch.setattr(business, "slots", availability)
    dialogue = Dialogue(business, model, EventBus())
    text = "Есть свободное время на консультацию завтра?"
    assert await dialogue.prepare(sid, text) is None
    assert calls == []
    reply = await dialogue.message(sid, text, uuid4())
    assert "13:00" in reply["text"] and "ещё не забронировано" in reply["text"]
    assert reply["response_source"] == "availability"
    slots.clear()
    assert "не найдено" in (await dialogue.message(sid, text, uuid4()))["text"]
    assert calls == [("consultation", date)] * 2
    for table in ("drafts", "actions", "bookings"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert model.calls == []


@pytest.mark.parametrize("channel", ["voice", "chat"])
async def test_extraction_channel_is_session_channel(business, session, channel):
    sid, model = session["uuid"], CountingModel()
    await business.pool.execute("UPDATE sessions SET channel=$2 WHERE id=$1", sid, channel)
    dialogue = Dialogue(business, model, EventBus())
    prepared = await dialogue.prepare(sid, "Обсудим необычный вопрос")
    await dialogue.message(sid, "Обсудим необычный вопрос", uuid4(), prepared=prepared)
    assert len(model.calls) == 1
    assert model.calls[0][2]["_channel"] == channel


@pytest.mark.parametrize("intent", ["conversation", "callback"])
async def test_unresolved_phone_correction_invalidates_only_on_final_message(business, session, intent):
    sid = session["uuid"]
    draft = await business.draft(sid, "callback", CallbackDraft(name="Анна", phone="+375290001234", message="Исходный вопрос"))

    class Model(CountingModel):
        async def extract(self, *args):
            self.calls.append(args)
            return ExtractedIntent(intent=intent, message="Другое сообщение", answer="Уточните номер.")

    model = Model()
    dialogue = Dialogue(business, model, EventBus())
    text = "Телефон исправьте на 80291234567"
    before = await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid)
    prepared = await dialogue.prepare(sid, text)
    assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == before
    assert await business.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(draft["id"])) == "awaiting_confirmation"
    reply = await dialogue.message(sid, text, uuid4(), prepared=prepared)
    assert "кодом страны" in reply["text"]
    expected = {key: value for key, value in before.items() if key != "phone"}
    assert reply["collected"] == expected
    assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == expected
    assert await business.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(draft["id"])) == "superseded"
    assert await business.active_draft(sid) is None
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 0
    assert len(model.calls) == 1


@pytest.mark.parametrize("change", ["consent", "permission"])
async def test_phone_clarification_checks_live_rights_before_clearing(business, session, change):
    sid = session["uuid"]
    draft = await business.draft(sid, "callback", CallbackDraft(name="Анна", phone="+375290001234", message="Исходный вопрос"))
    before = await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid)

    class Model:
        async def extract(self, *args):
            if change == "consent":
                await business.pool.execute("UPDATE sessions SET consent=false WHERE id=$1", sid)
            else:
                await business.pool.execute("UPDATE sessions SET agent_config=$2 WHERE id=$1", sid, {"allowed_actions": []})
            return ExtractedIntent(intent="conversation", answer="Уточните номер")

    with pytest.raises(AppError):
        await Dialogue(business, Model(), EventBus()).message(sid, "Телефон исправьте на 80291234567", uuid4())
    assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == before
    assert await business.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(draft["id"])) == "awaiting_confirmation"
