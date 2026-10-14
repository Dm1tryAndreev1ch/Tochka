import json

import httpx
import pytest

from app.config import settings
from app.errors import AppError
from app.llm_schema import guard_intent_output, intent_schema
from app.models import LocalModels

SERVICES = [
    {"id": "diagnostics", "name": "Диагностика", "description": "Проверка", "price": 10, "duration": 30},
    {"id": "consultation", "name": "Консультация", "description": "Совет", "price": 20, "duration": 30},
]


def booking_branch(text, context=None):
    return next(
        item for item in intent_schema(text, context or {}, SERVICES, {})["oneOf"]
        if item["properties"]["intent"]["const"] == "booking"
    )


@pytest.mark.parametrize("text", [
    "Хотел бы прийти на Диагностика в один из ближайших дней после обеда; меня зовут Алексей. "
    "Точную дату и часы ещё не выбрал.",
    "Запишите на 16 октября 2026 года.",
    "Запишите на 16.10.2026.",
    "Перенесите на два дня позже.",
    "Перенесите на два часа позже.",
    "Мне удобно в один день с консультацией.",
    "Хочу прийти на полдня.",
    "Лучше в поликлинику после обеда.",
    "В пятнадцать минут не уложусь.",
    "Запишите в 29:15.",
])
def test_dates_durations_and_vague_phrases_cannot_enable_a_guessed_time(text):
    assert "time" not in booking_branch(text)["properties"]


@pytest.mark.parametrize("text", [
    "Перенесите начало на 09:15, это ровно на пятнадцать минут позже.",
    "Запишите завтра в десять утра.",
    "Можно в одиннадцать?",
    "Лучше на двадцать один час.",
    "Запишите в 14 на диагностику.",
    "В 11 45.",
    "В десять тридцать.",
    "В десять сорок пять минут.",
    "В полчетвёртого.",
    "В половине третьего.",
    "Без четверти три.",
    "В полдень.",
])
def test_explicit_clock_expressions_allow_time_extraction(text):
    assert "time" in booking_branch(text)["properties"]


def test_pending_time_accepts_bare_hour_without_mistaking_it_for_a_name():
    branch = booking_branch(
        "Девять", {"intent": "booking", "service_id": "diagnostics", "date": "2026-10-16"},
    )
    assert set(branch["properties"]) == {"intent", "time"}
    assert "time" not in booking_branch("девять", {"intent": "booking"})["properties"]


def test_time_correction_cannot_copy_service_from_existing_booking():
    branch = booking_branch(
        "Перенесите начало на 09:15, это ровно на пятнадцать минут позже. "
        "Остальные данные те же, пока без подтверждения.",
        {"intent": "booking", "service_id": "diagnostics", "name": "Алексей", "time": "09:00"},
    )
    assert set(branch["properties"]) == {"intent", "time"}


def test_service_choices_are_limited_to_services_actually_mentioned():
    branch = booking_branch("Хочу диагностику.")
    assert branch["properties"]["service_id"]["enum"] == ["diagnostics"]
    assert "service_id" in branch["required"]
    branch = booking_branch("Хочу диагностику или консультацию.")
    assert branch["properties"]["service_id"]["enum"] == ["diagnostics", "consultation"]


@pytest.mark.parametrize("output", [
    {"intent": "booking", "service_id": "consultation"},
    {"intent": "booking"},
    {"service_id": "diagnostics"},
    {"intent": "invented"},
    [],
])
def test_guard_rejects_an_invalid_branch_or_spoken_service_instead_of_substituting(output):
    with pytest.raises(ValueError):
        guard_intent_output(output, intent_schema("Хочу диагностику.", {}, SERVICES, {}))


@pytest.mark.parametrize("text,output", [
    ("Меня зовут Анна.", {"intent": "booking", "name": "Ольга"}),
    ("Телефон +375290000001.", {"intent": "booking", "phone": "+375290000002"}),
    ("Начало в 09:15.", {"intent": "booking", "time": "29:99"}),
])
def test_guard_rejects_wrong_explicit_identity_and_malformed_time(text, output):
    with pytest.raises(ValueError):
        guard_intent_output(output, intent_schema(text, {}, SERVICES, {}))


async def test_compact_response_cannot_overwrite_unmentioned_data_if_server_ignores_schema(monkeypatch):
    monkeypatch.setattr(settings, "llm_prompt_profile", "compact")

    async def handler(request):
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "booking", "time": "09:15", "service_id": "consultation",
            "name": "Чужое Имя", "phone": "+375290000099", "date": "2026-10-20",
        })}}]})

    models = LocalModels()
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await models.extract(
            "Перенесите начало на 09:15, остальные данные те же.", [],
            {"intent": "booking", "service_id": "diagnostics", "name": "Анна"}, SERVICES, [], "2026-10-13",
        )
        assert result.model_dump(exclude_defaults=True) == {"intent": "booking", "time": "09:15"}
    finally:
        await models.close()


async def test_compact_response_rejects_wrong_named_service(monkeypatch):
    monkeypatch.setattr(settings, "llm_prompt_profile", "compact")
    models = LocalModels()
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": '{"intent":"booking","service_id":"consultation"}'}}]},
    )))
    try:
        with pytest.raises(AppError) as error:
            await models.extract("Хочу диагностику.", [], {}, SERVICES, [], "2026-10-13")
        assert error.value.code == "llm_unavailable"
    finally:
        await models.close()


@pytest.mark.parametrize("profile,context,content,expected", [
    ("compact", {"_classification_only": True, "_category_schema": {"booking": "Запись"}},
     '{"category":"booking"}', {"category": "booking"}),
    ("legacy", {}, '{"intent":"booking","service_id":"diagnostics"}',
     {"intent": "booking", "service_id": "diagnostics"}),
])
async def test_compact_guard_does_not_change_other_response_contracts(monkeypatch, profile, context, content, expected):
    monkeypatch.setattr(settings, "llm_prompt_profile", profile)
    models = LocalModels()
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]},
    )))
    try:
        result = await models.extract("Продолжим.", [], context, SERVICES, [], "2026-10-13")
        assert result.model_dump(exclude_defaults=True) == expected
    finally:
        await models.close()


@pytest.mark.parametrize("context", [
    {"intent": "booking", "service_id": "diagnostics", "date": "2026-10-16", "time": "11:00"},
    {"intent": "callback", "message": "Уточнить срок ремонта"},
])
@pytest.mark.parametrize("text", ["анна", "анна иванова", "Я Анна."])
def test_pending_name_allows_semantic_extraction_without_forcing_input_as_name(context, text):
    schema = intent_schema(text, context, SERVICES, {})
    branch = next(item for item in schema["oneOf"] if item["properties"]["intent"]["const"] == context["intent"])
    assert "name" in branch["properties"]
    assert "enum" not in branch["properties"]["name"]
    assert "name" not in branch["required"]
    assert guard_intent_output({"intent": context["intent"], "name": "Анна"}, schema)["name"] == "Анна"


@pytest.mark.parametrize("context", [
    {"intent": "booking"},
    {"intent": "booking", "service_id": "diagnostics"},
    {"intent": "booking", "service_id": "diagnostics", "date": "2026-10-16"},
    {"intent": "callback"},
    {"intent": "callback", "message": "Ремонт", "name": "Ольга"},
])
def test_optional_name_is_not_enabled_before_the_name_question(context):
    assert "name" not in booking_branch("анна", context)["properties"]


@pytest.mark.parametrize("text", [
    "Через день.", "Через неделю.", "Спустя неделю.", "Через сутки.",
    "14-го", "четырнадцатого", "Четырнадцатого", "Двадцать первого.",
])
def test_explicit_relative_or_ordinal_date_can_be_resolved_without_collecting_a_name(text):
    context = {"intent": "booking", "service_id": "diagnostics", "date": "2026-10-16", "time": "11:00"}
    branch = booking_branch(text, context)
    assert "date" in branch["properties"]
    assert "name" not in branch["properties"]
    assert "time" not in branch["properties"]


@pytest.mark.parametrize("text", [
    "В 9 часов 15 минут.", "В девять часов пятнадцать минут.",
    "Девять тридцать.", "9 30", "В девять часов и пятнадцать минут.",
])
def test_full_hour_and_minute_phrases_allow_only_time_when_time_is_pending(text):
    branch = booking_branch(text, {"intent": "booking", "service_id": "diagnostics", "date": "2026-10-16"})
    assert set(branch["properties"]) == {"intent", "time"}


@pytest.mark.parametrize("text", ["В половине третьего.", "В четверть третьего.", "В полчетвёртого."])
def test_clock_ordinals_do_not_enable_a_calendar_date(text):
    assert set(booking_branch(text)["properties"]) == {"intent", "time"}


@pytest.mark.parametrize("context", [{}, {"intent": "booking"}, {"intent": "callback"}])
def test_explicit_name_introductions_remain_available_before_other_fields(context):
    branch = booking_branch("Меня зовут Анна.", context)
    assert branch["properties"]["name"] == {"type": "string", "enum": ["Анна"]}
    assert "name" in branch["required"]


def test_phone_correction_does_not_force_the_first_rejected_number():
    text = "Исправьте телефон: не +375290000001, а +375290000002."
    schema = intent_schema(text, {"intent": "booking", "phone": "+375290000001"}, SERVICES, {})
    branch = booking_branch(text, {"intent": "booking", "phone": "+375290000001"})
    assert branch["properties"]["phone"]["enum"] == ["+375290000002"]
    assert guard_intent_output({"intent": "booking", "phone": "+375290000002"}, schema)["phone"] == "+375290000002"
    with pytest.raises(ValueError):
        guard_intent_output({"intent": "booking", "phone": "+375290000001"}, schema)


def test_two_positive_phones_are_not_silently_reduced_to_the_first_one():
    branch = booking_branch("Можно связаться по +375290000001 или по +375290000002.")
    assert branch["properties"]["phone"]["enum"] == ["+375290000001", "+375290000002"]
    assert "phone" not in branch["required"]


def test_rejected_old_phone_without_a_replacement_does_not_allow_a_guessed_phone():
    branch = booking_branch("Исправьте телефон, вместо +375290000001 нужен другой номер.")
    assert "phone" not in branch["properties"]
