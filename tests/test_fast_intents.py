from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.fast_intents import extract_common, pending_customer_name, published_address, spoken_name
from app.speech import normalize, segments

SERVICES = [{"id": "consultation", "name": "Консультация"}]
NOW = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Europe/Minsk"))


def test_common_booking_extracts_only_spoken_fields():
    result = extract_common(
        "Хочу записаться на консультацию завтра в 11:00. Меня зовут Александр. Телефон +375291234567.",
        {},
        SERVICES,
        NOW,
    )
    assert result.service_id == "consultation"
    assert result.date == "2026-10-13"
    assert result.time == "11:00"
    assert result.name == "Александр"
    assert result.phone == "+375291234567"


def test_name_correction_never_collects_the_command_as_a_customer_name():
    result = extract_common("Имя исправьте на Ольга Тестовая.", {"intent": "booking", "name": "Анна"}, SERVICES, NOW)
    assert result.name == "Ольга Тестовая"
    assert spoken_name("Имя исправьте") is None


def test_full_callback_name_before_phone_is_explicit():
    result = extract_common("Попросите сотрудника перезвонить мне. Виктор Тестов, +375290000006.", {}, SERVICES, NOW)
    assert result.intent == "callback"
    assert result.name == "Виктор Тестов"


def test_date_does_not_invent_time():
    result = extract_common("Запишите на консультацию на 13.10.2026.", {}, SERVICES, NOW)
    assert result.date == "2026-10-13"
    assert result.time is None


@pytest.mark.parametrize("text", [
    "Запишите на консультацию на 16 октября 2026 года.",
    "Запишите на консультацию 2026-10-16.",
])
def test_explicit_calendar_dates_preserve_the_spoken_day_without_a_time(text):
    result = extract_common(text, {"intent": "booking"}, SERVICES, NOW)
    assert result.date == "2026-10-16"
    assert result.time is None
    assert result.name is None


@pytest.mark.parametrize("text", [
    "Запишите на консультацию на 16.10.",
    "Запишите на консультацию на 16/10/2026.",
    "Перенесите на два часа позже.",
    "Лучше в десять тридцать.",
    "Лучше в девять или в десять.",
])
def test_unparsed_dates_and_compound_times_require_semantic_extraction(text):
    assert extract_common(text, {"intent": "booking"}, SERVICES, NOW) is None


@pytest.mark.parametrize("text", [
    "Запишите на консультацию в один из ближайших дней.",
    "Запишите на консультацию в один день с диагностикой.",
    "Запишите на консультацию на два дня.",
])
def test_vague_date_and_duration_never_become_a_clock_time(text):
    result = extract_common(text, {}, SERVICES, NOW)
    assert result is None or result.time is None


def test_vague_phrase_cannot_override_a_later_explicit_clock():
    result = extract_common("Запишите в один из дней, лучше в два.", {}, SERVICES, NOW)
    assert result.time == "02:00"


@pytest.mark.parametrize("text,expected", [("девять", "09:00"), ("Девять.", "09:00"), ("09:15", "09:15")])
def test_direct_answer_to_booking_time_question_is_preserved(text, expected):
    result = extract_common(
        text, {"intent": "booking", "service_id": "consultation", "date": "2026-10-16"}, SERVICES, NOW,
    )
    assert result.time == expected
    assert result.name is None


def test_bare_word_number_is_not_a_clock_without_a_pending_time_question():
    result = extract_common("девять", {"intent": "booking"}, SERVICES, NOW)
    assert result is None or result.time is None


@pytest.mark.parametrize(
    "text",
    [
        "Не хочу записаться на консультацию",
        "Запишите завтра в полчетвертого",
        "На следующую пятницу после обеда",
        "Лучше в 11 45",
    ],
)
def test_ambiguous_or_negative_input_goes_to_model(text):
    assert extract_common(text, {"intent": "booking"}, SERVICES, NOW) is None


def test_correction_replaces_time_without_guessing_date():
    result = extract_common(
        "Нет, лучше в двенадцать.", {"intent": "booking", "date": "2026-10-13"}, SERVICES, NOW
    )
    assert result.time == "12:00"
    assert result.date is None


def test_phone_never_gets_country_added_or_truncated():
    result = extract_common("Запишите, телефон 291234567", {}, SERVICES, NOW)
    assert result.phone is None
    result = extract_common("Запишите, телефон +375291234567999999999999999", {}, SERVICES, NOW)
    assert result.phone is None


def test_address_uses_only_the_published_content_supplied():
    assert published_address("Какой адрес?", []) is None
    assert (
        published_address(
            "Где вы находитесь?", [{"content": "Тестовая компания. Адрес: Минск, Примерная, 10."}]
        )
        == "Адрес: Минск, Примерная, 10."
    )


def test_speech_dates_prices_times_are_russian_words():
    result = normalize("13.10.2026 в 11:00, 30 BYN")
    assert "тринадцатого октября" in result
    assert "две тысячи двадцать шестого года" in result
    assert "одиннадцать часов" in result
    assert "тридцать белорусских рублей" in result


@pytest.mark.parametrize(
    "price,expected",
    [
        (1, "один белорусский рубль"),
        (2, "два белорусских рубля"),
        (11, "одиннадцать белорусских рублей"),
        (21, "двадцать один белорусский рубль"),
    ],
)
def test_currency_inflection(price, expected):
    assert normalize(f"{price} BYN") == expected


def test_utterance_segments_never_drop_long_words():
    text = "начало " + ("а" * 800) + " конец"
    parts = list(segments(text))
    assert all(len(part) <= 350 for part in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


@pytest.mark.parametrize("text", [
    "Через день.", "Через неделю.", "Спустя неделю.", "Через сутки.",
    "14-го", "четырнадцатого", "Четырнадцатого", "Двадцать первого.",
])
def test_relative_and_ordinal_dates_require_semantic_resolution_without_becoming_names(text):
    for context in (
        {"intent": "booking", "service_id": "consultation"},
        {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00"},
    ):
        assert extract_common(text, context, SERVICES, NOW) is None


@pytest.mark.parametrize("text", [
    "В 9 часов 15 минут.", "В девять часов пятнадцать минут.",
    "Девять тридцать.", "9 30", "В девять часов и пятнадцать минут.",
])
def test_compound_clock_is_never_partially_parsed_as_an_hour_or_a_name(text):
    context = {"intent": "booking", "service_id": "consultation", "date": "2026-10-16"}
    assert extract_common(text, context, SERVICES, NOW) is None


@pytest.mark.parametrize("context", [
    {"intent": "booking"},
    {"intent": "booking", "service_id": "consultation"},
    {"intent": "booking", "service_id": "consultation", "date": "2026-10-16"},
    {"intent": "callback"},
])
def test_bare_word_is_not_a_name_before_the_name_question(context):
    assert extract_common("Анна", context, SERVICES, NOW) is None


@pytest.mark.parametrize("context", [
    {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00"},
    {"intent": "callback", "message": "Уточнить ремонт"},
])
def test_bare_capitalized_name_is_still_fast_when_name_is_actually_pending(context):
    assert extract_common("Анна", context, SERVICES, NOW).name == "Анна"


@pytest.mark.parametrize("text,expected", [("В двадцать один.", "21:00"), ("двадцать два", "22:00")])
def test_two_word_hour_is_not_mistaken_for_hour_and_minutes(text, expected):
    context = {"intent": "booking", "service_id": "consultation", "date": "2026-10-16"}
    assert extract_common(text, context, SERVICES, NOW).time == expected


@pytest.mark.parametrize("context", [{}, {"intent": "booking"}, {"intent": "callback"}])
def test_explicit_introduction_is_preserved_before_pending_name(context):
    text = "Запишите меня, меня зовут Анна." if not context else "Меня зовут Анна."
    assert extract_common(text, context, SERVICES, NOW).name == "Анна"


@pytest.mark.parametrize("context", [
    {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00"},
    {"intent": "callback", "message": "Уточнить срок ремонта"},
])
@pytest.mark.parametrize("text,expected", [
    ("анна", "Анна"),
    ("Я Анна.", "Анна"),
    ("я анна иванова", "Анна Иванова"),
    ("АННА ИВАНОВНА ИВАНОВА!", "Анна Ивановна Иванова"),
    ("  семён   петров  ", "Семён Петров"),
    ("Я John McDonald.", "John McDonald"),
    ("Анна-Мария", "Анна-Мария"),
    ("АННА-МАРИЯ", "Анна-Мария"),
])
def test_pending_name_answer_is_collected_without_a_model_request(context, text, expected):
    result = extract_common(text, context, SERVICES, NOW)
    assert result.model_dump(exclude_defaults=True) == {"intent": context["intent"], "name": expected}


@pytest.mark.parametrize("text", [
    "нет", "ДА", "Спасибо.", "не хочу", "Отмена", "Позже", "Я не скажу", "Я передумала",
    "подтверждаю", "Анна?", "Оператор", "Телефон", "Четырнадцатого", "Спустя неделю",
    "Среда", "Завтра", "Девять", "Девять тридцать", "В девять часов пятнадцать минут",
    "09:30", "16.10.2026", "Консультация", "Я Консультация", "МРТ", "Анна 123",
    "Анна, не Ольга", "Анна Ивановна Иванова Петрова",
    "Как вас зовут", "Кто вы", "Скажите адрес", "Мне неудобно", "Когда можно",
])
def test_control_answers_questions_and_other_fields_are_not_collected_as_names(text):
    context = {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00"}
    services = [*SERVICES, {"id": "mri", "name": "МРТ"}]
    assert pending_customer_name(text, context, services) is None
    result = extract_common(text, context, services, NOW)
    assert result is None or result.name is None


@pytest.mark.parametrize("context", [
    {},
    {"intent": "booking", "service_id": "consultation"},
    {"intent": "booking", "service_id": "consultation", "date": "2026-10-16"},
    {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00", "name": "Ольга"},
    {"intent": "callback"},
    {"intent": "callback", "message": "Ремонт", "name": "Ольга"},
])
@pytest.mark.parametrize("text", ["анна", "Я Анна."])
def test_direct_name_collection_is_disabled_without_an_outstanding_name_question(context, text):
    assert pending_customer_name(text, context, SERVICES) is None
    assert extract_common(text, context, SERVICES, NOW) is None


def test_pending_name_does_not_replace_existing_handoff_or_negation_routes():
    context = {"intent": "booking", "service_id": "consultation", "date": "2026-10-16", "time": "11:00"}
    assert extract_common("Соедините с оператором", context, SERVICES, NOW).intent == "handoff"
    assert extract_common("Не хочу записываться", context, SERVICES, NOW) is None
