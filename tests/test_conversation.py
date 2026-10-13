from uuid import UUID, uuid4

from app.dialogue import Dialogue, EventBus
from app.schemas import ExtractedIntent


class ConversationModel:
    def __init__(self):
        self.calls = []

    async def extract(self, text, history, context, *args):
        self.calls.append((text, history, context))
        return ExtractedIntent(intent="conversation", answer="Понимаю. Давайте обсудим ваш вопрос подробнее.")


async def test_conversation_keeps_name_after_draft_cancellation_and_passes_history(business):
    model = ConversationModel()
    dialogue = Dialogue(business, model, EventBus())
    session = await business.new_session("chat", True, "female")
    sid = UUID(session["id"])
    await dialogue.message(sid, "Меня зовут Алексей. Хотел бы просто поговорить.", uuid4())
    assert model.calls[-1][2]["customer_memory"]["customer_name"] == "Алексей"
    assert model.calls[-1][2]["assistant_name"] == "Мария"
    for i in range(18):
        await dialogue.message(sid, f"Продолжим обсуждение, реплика {i}", uuid4())
    await dialogue.message(sid, "отмена", uuid4())
    await dialogue.message(sid, "Ты помнишь моё имя?", uuid4())
    assert model.calls[-1][2]["customer_memory"]["customer_name"] == "Алексей"
    assert len(model.calls[-1][1]) == 32
    assert await business.pool.fetchval("SELECT count(*) FROM bookings") == 0
    assert await business.pool.fetchval("SELECT count(*) FROM requests") == 0
    other = await business.new_session("chat", True)
    await dialogue.message(UUID(other["id"]), "Давай познакомимся", uuid4())
    assert model.calls[-1][2]["customer_memory"] == {}
