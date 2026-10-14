import json

import httpx
import pytest

from app.config import settings
from app.errors import AppError
from app.models import LocalModels


@pytest.mark.parametrize("channel,text,limit", [
    ("voice", "Как подготовиться к сложному разговору?", 180),
    ("voice", "Объясни подробнее подготовку к разговору.", 400),
    ("chat", "Как подготовиться к сложному разговору?", 400),
])
async def test_voice_limits_only_the_default_conversation_answer(monkeypatch, channel, text, limit):
    monkeypatch.setattr(settings, "llm_prompt_profile", "compact")
    monkeypatch.setattr(settings, "llm_voice_answer_chars", 180)

    def handler(request):
        payload = json.loads(request.content)
        branches = payload["response_format"]["schema"]["oneOf"]
        conversation = next(b for b in branches if b["properties"]["intent"]["const"] == "conversation")
        assert conversation["properties"]["answer"]["maxLength"] == limit
        # Business extraction keeps its complete response budget.
        assert payload["max_tokens"] == settings.llm_reply_tokens
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "conversation", "answer": "Определите цель разговора и подготовьте три открытых вопроса по главным темам.",
        }, ensure_ascii=False)}}]})

    model = LocalModels()
    await model.http.aclose()
    model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        answer = await model.extract(text, [], {"_channel": channel}, [], [], "2026-10-13")
        assert "три открытых вопроса" in answer.answer
    finally:
        await model.close()


async def test_voice_guard_rejects_a_server_that_ignores_the_short_answer_limit(monkeypatch):
    monkeypatch.setattr(settings, "llm_prompt_profile", "compact")
    model = LocalModels()
    await model.http.aclose()
    model.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"intent": "conversation", "answer": "Слово " * 50})}}],
    })))
    try:
        with pytest.raises(AppError) as error:
            await model.extract("Объясни смысл вопроса.", [], {"_channel": "voice"}, [], [], "2026-10-13")
        assert error.value.code == "llm_unavailable"
    finally:
        await model.close()
