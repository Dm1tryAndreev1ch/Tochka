from uuid import UUID, uuid4

import pytest

from app.dialogue import Dialogue, EventBus
from app.grounding import grounded_reply
from app.schemas import ExtractedIntent


class UntrustedModel:
    async def extract(self, *args):
        return ExtractedIntent(intent="conversation", answer="Позвоните +375291234567.")


async def test_spoken_request_for_human_changes_session_state(business):
    await business.pool.execute("UPDATE settings SET value='{\"available\":true}' WHERE key='operator'")
    data = await business.new_session("chat", True)
    sid = UUID(data["id"])
    dialogue = Dialogue(business, UntrustedModel(), EventBus())
    reply = await dialogue.message(sid, "Соедини меня с живым сотрудником, пожалуйста", uuid4())
    assert reply["handoff"]["status"] == "pending"
    assert await business.pool.fetchval("SELECT state FROM sessions WHERE id=$1", sid) == "handoff_pending"


async def test_company_phone_cannot_be_invented_by_model(business):
    data = await business.new_session("chat", True)
    dialogue = Dialogue(business, UntrustedModel(), EventBus())
    reply = await dialogue.message(UUID(data["id"]), "Какой у вашей компании телефон?", uuid4())
    assert "+375291234567" not in reply["text"]
    assert reply["response_source"] == "missing_fact"


async def test_name_recall_uses_session_memory_and_does_not_leak(business):
    dialogue = Dialogue(business, UntrustedModel(), EventBus())
    data = await business.new_session("chat", True)
    sid = UUID(data["id"])
    await dialogue.message(sid, "Меня зовут Алексей", uuid4())
    reply = await dialogue.message(sid, "Как меня зовут?", uuid4())
    assert "Алексей" in reply["text"]
    other = await business.new_session("chat", True)
    reply = await dialogue.message(UUID(other["id"]), "Как меня зовут?", uuid4())
    assert "Алексей" not in reply["text"]


def test_factual_answer_only_returns_validated_source_excerpt():
    facts = [{"content": "Гарантия действует тридцать дней."}]
    bad = ExtractedIntent(intent="faq", answer="Гарантия год.", fact_quotes=["Гарантия действует год."])
    assert grounded_reply(bad, "Какая гарантия?", [], facts)["response_source"] == "missing_fact"
    valid = ExtractedIntent(intent="faq", answer="Гарантия год.", fact_quotes=[facts[0]["content"]])
    assert grounded_reply(valid, "Какая гарантия?", [], facts)["text"] == facts[0]["content"]


@pytest.mark.parametrize("quotes", [["", ""], [" "], ["\n"], ["Гарантия действует тридцать дней.", ""]])
def test_empty_quote_cannot_be_spoken_as_a_verified_company_fact(quotes):
    facts = [{"content": "Гарантия действует тридцать дней.\n Обратитесь к сотруднику."}]
    extracted = ExtractedIntent(intent="faq", fact_quotes=quotes)
    reply = grounded_reply(extracted, "Какая гарантия?", [], facts)
    assert reply["response_source"] == "missing_fact"
    assert reply["text"].strip()


async def test_unplayed_voice_answer_is_not_claimed_as_heard(business):
    class Model:
        async def extract(self, *args):
            return ExtractedIntent(intent="conversation", answer="Давайте обсудим ваш вопрос. С чего начнём?")

    data = await business.new_session("voice", True)
    sid = UUID(data["id"])
    dialogue = Dialogue(business, Model(), EventBus())
    reply = await dialogue.message(sid, "Хочу поговорить", uuid4())
    row = await business.pool.fetchrow("SELECT text FROM turns WHERE session_id=$1 AND role='assistant'", sid)
    assert row["text"] == ""
    assert reply["delivery"] == "pending"


async def test_late_playback_receipt_cannot_restore_interrupted_answer(business):
    class Model:
        async def extract(self, *args):
            return ExtractedIntent(intent="conversation", answer="Первое предложение. Второе предложение.")

    data = await business.new_session("voice", True)
    sid, key = UUID(data["id"]), uuid4()
    dialogue = Dialogue(business, Model(), EventBus())
    reply = await dialogue.message(sid, "Хочу поговорить", key)
    prefix = len("Первое предложение.")
    await dialogue.audio_sent(sid, key, len(reply["text"]))
    await dialogue.acknowledge_playback(sid, key, prefix)
    await dialogue.interrupt_playback(sid)
    late = await dialogue.acknowledge_playback(sid, key, len(reply["text"]))
    assert late == {"delivery": "interrupted", "played_chars": prefix}
    assert await business.pool.fetchval("SELECT text FROM turns WHERE session_id=$1 AND role='assistant'", sid) == "Первое предложение."
