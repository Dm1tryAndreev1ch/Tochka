from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.fast_intents import extract_common, published_address
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


def test_date_does_not_invent_time():
    result = extract_common("Запишите на консультацию на 13.10.2026.", {}, SERVICES, NOW)
    assert result.date == "2026-10-13"
    assert result.time is None


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
