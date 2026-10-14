"""Готовые ответы о Точке: срабатывают на справочные вопросы и не перехватывают действия."""

import json
from pathlib import Path

import pytest

from app import company_faq
from app.company_faq import ANSWERS, COMPILED, company_reply, company_topic

SUITE = json.loads((Path(__file__).parent / "fixtures/fast_receptionist_routing.json").read_text())


@pytest.mark.parametrize("text,topic", [
    ("Что такое Точка?", "about_product"),
    ("Расскажите про проект Точка", "about_product"),
    ("Для чего ты нужен?", "about_product"),
    ("Расскажи о вашей компании", "about_company"),
    ("Чем вы занимаетесь?", "about_company"),
    ("Кто вы такие?", "about_company"),
    ("Что ты умеешь?", "capabilities"),
    ("Что вы можете?", "capabilities"),
    ("Что умеет Точка?", "capabilities"),
    ("Что умеет этот ассистент?", "capabilities"),
    ("Точка что умеет?", "capabilities"),
    ("Какие у тебя функции?", "capabilities"),
    ("Как это работает?", "how_it_works"),
    ("Как проходит запись?", "how_it_works"),
    ("Что такое бета-тест?", "beta_what"),
    ("Расскажи про бета тест", "beta_what"),
    ("Как записаться на бета-тест?", "beta_how"),
    ("Как мне принять участие?", "beta_how"),
    ("Сколько стоит бета-тест?", "beta_conditions"),
    ("Бета-тест бесплатный?", "beta_conditions"),
    ("Когда начинается бета тест", "beta_conditions"),
    ("Кому подходит бета-тест?", "beta_why_me"),
    ("Что я получу от участия в бета-тесте?", "beta_why_me"),
    ("Какие данные вам нужны?", "data_needed"),
    ("Что нужно назвать для записи?", "data_needed"),
    ("А что если ИИ неправильно запишет мой номер?", "wrong_record"),
    ("Что если ты неправильно поймёшь моё имя?", "wrong_record"),
    ("Как исправить запись?", "wrong_record"),
    ("Можно ли перепроверить данные?", "wrong_record"),
    ("А если вы не расслышите?", "misheard"),
    ("У меня акцент, ты поймёшь?", "misheard"),
    ("Как вы защищаете персональные данные?", "privacy"),
    ("Это безопасно?", "privacy"),
    ("Не будет утечки данных?", "privacy"),
    ("Мои данные в безопасности?", "privacy"),
    ("Куда уходят мои данные?", "privacy"),
    ("Мои данные уходят в облако?", "local_processing"),
    ("Вы отправляете разговор в ChatGPT?", "local_processing"),
    ("Что такое изолированные контуры?", "contours"),
    ("А блокчейн у вас есть?", "blockchain"),
    ("Можно ли подделать журнал?", "blockchain"),
    ("Кто имеет доступ к моим данным?", "who_sees"),
    ("Сотрудники слышат разговор?", "who_sees"),
    ("Как удалить мои данные?", "delete_data"),
    ("Хочу отозвать согласие", "delete_data"),
    ("Вы записываете разговор?", "recording"),
    ("Мой разговор сохраняется?", "recording"),
    ("Как долго вы храните данные?", "retention"),
    ("Вы будете мне звонить с рекламой?", "contacts_use"),
    ("Зачем вам мой телефон?", "contacts_use"),
    ("Я боюсь оставлять данные роботу", "fear_ai"),
    ("Я не доверяю ИИ", "fear_ai"),
    ("Вы человек?", "is_ai"),
    ("Я говорю с роботом?", "is_ai"),
    ("Ты живой?", "is_ai"),
    ("Чем ты отличаешься от обычного бота?", "vs_bot"),
    ("Вы работаете в телеграме?", "where_works"),
    ("Куда попадает моя заявка?", "integrations"),
    ("Есть интеграция с CRM?", "integrations"),
    ("На каких языках вы говорите?", "languages"),
    ("Вы работаете круглосуточно?", "availability"),
    ("Какие у тебя ограничения?", "limits"),
])
def test_reference_questions_get_a_canned_answer(text, topic):
    assert company_topic(text) == topic
    reply = company_reply(text)
    assert reply["response_source"] == "company_faq"
    assert reply["text"] == company_reply(text)["text"]
    assert "{" not in reply["text"]


@pytest.mark.parametrize("text", [
    "Запишите меня на бета-тест",
    "Хочу записаться на бета-тест завтра в 11:00",
    "Меня зовут Иван, мой телефон +375291234567",
    "Запишите меня на консультацию",
    "Перезвоните мне",
    "Соедините с оператором",
    "Позовите сотрудника",
    "Иван",
    "Завтра в 15:00",
    "Да",
    "Нет, не так",
    "Сколько стоит консультация?",
    "Какой у вас адрес?",
    "Какая гарантия на услугу?",
    "Работаете ли вы в субботу?",
    "Как записаться на консультацию?",
])
def test_actions_and_ordinary_requests_are_not_intercepted(text):
    assert company_topic(text) is None
    assert company_reply(text) is None


def test_long_text_goes_to_the_normal_path():
    assert company_topic("Расскажите про безопасность " + "очень " * 40 + "подробно") is None


def test_every_topic_has_an_answer_and_every_answer_is_reachable():
    topics = {topic for topic, _ in COMPILED}
    assert topics <= set(ANSWERS)
    assert set(ANSWERS) - topics == set()


def test_answers_stay_short_enough_for_speech_and_plain_text():
    for key, text in ANSWERS.items():
        assert len(text) < 700, key
        assert not any(mark in text for mark in ("**", "#", "\n- ", "http")), key


def test_beta_conditions_are_never_invented(monkeypatch):
    for key in ("beta_period", "beta_price", "beta_places"):
        monkeypatch.setitem(company_faq.FACTS, key, None)
    text = company_reply("Сколько стоит бета-тест?")["text"]
    assert "уточнит сотрудник" in text
    monkeypatch.setitem(company_faq.FACTS, "beta_price", "бесплатно")
    assert "бесплатно" in company_reply("Сколько стоит бета-тест?")["text"]


def test_notification_sentence_only_when_the_feature_is_enabled(monkeypatch):
    monkeypatch.setitem(company_faq.FACTS, "customer_notification", False)
    assert "уведомление" not in company_reply("А если ты неправильно запишешь?")["text"]
    monkeypatch.setitem(company_faq.FACTS, "customer_notification", True)
    assert "уведомление" in company_reply("А если ты неправильно запишешь?")["text"]


def test_blockchain_answer_never_claims_personal_data_is_on_chain():
    text = company_reply("Вы используете блокчейн?")["text"].lower()
    assert "не попадают" in text


def test_beta_booking_how_to_is_hidden_when_booking_is_disabled():
    assert company_reply("Как записаться на бета-тест?", allowed_actions=["handoff"]) is None


def test_reference_suite_questions_are_not_shadowed():
    for case in SUITE.get("cases", []):
        text = case.get("text") or case.get("utterance")
        if text and case.get("expected_source") in {"knowledge", "missing_fact"}:
            assert company_topic(text) is None, text


class _NormalPath(Exception):
    pass


class _BusinessThatMustNotBeReached:
    pool = None

    async def active_draft(self, sid):
        raise _NormalPath


async def test_dialogue_answers_company_questions_without_business_or_model():
    from uuid import uuid4

    from app.dialogue import Dialogue, EventBus

    dialogue = Dialogue(_BusinessThatMustNotBeReached(), None, EventBus())  # the shortcut must answer alone
    for text in ("Что умеет Точка?", "А что если ИИ неправильно запишет?", "Вы используете блокчейн?"):
        reply = await dialogue.respond(uuid4(), text, {}, uuid4())
        assert reply["response_source"] == "company_faq"


async def test_dialogue_with_custom_instructions_keeps_the_model_path():
    from uuid import uuid4

    from app.dialogue import Dialogue, EventBus

    dialogue = Dialogue(_BusinessThatMustNotBeReached(), None, EventBus())
    with pytest.raises(_NormalPath):
        await dialogue.respond(uuid4(), "Что умеет Точка?", {}, uuid4(), agent_config={"instructions": "Свои правила."})


def test_project_facts_match_the_honesty_rules():
    from app.company_faq import PROJECT_FACTS

    lowered = PROJECT_FACTS.lower()
    assert "не публикуются в блокчейне" in lowered
    assert "полностью защищ" not in lowered and "полностью изолир" not in lowered
    assert len(PROJECT_FACTS) < 2000


def test_regression_phrasings_from_real_conversations():
    for text, topic in [
        ("Привет, а что вы вообще делаете?", "capabilities"),
        ("Меня беспокоит, что вы храните мои данные", "privacy"),
        ("Как мне проверить, что вы записали правильно?", "wrong_record"),
        ("Вы всё записываете?", "recording"),
        ("А можно удалить всё, что я сказал?", "delete_data"),
        ("Как это вообще работает", "how_it_works"),
        ("Это точно безопасно для моих данных?", "privacy"),
    ]:
        assert company_topic(text) == topic, text
    for text in ("Безопасная парковка у вас есть?", "Мне нужна защита от солнца"):
        assert company_topic(text) is None, text
