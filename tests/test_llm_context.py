import json

import httpx
import pytest

from app.config import settings
from app.errors import AppError
from app.models import LocalModels


async def test_long_conversation_is_bounded_before_model_request():
    calls = []
    async def handler(request):
        calls.append(request.url.path)
        data = json.loads(request.content)
        if request.url.path.endswith("/apply-template"):
            return httpx.Response(200, json={"prompt": "\n".join(m["content"] for m in data["messages"])})
        if request.url.path.endswith("/tokenize"):
            return httpx.Response(200, json={"tokens": list(range(len(data["content"])))})
        assert sum(len(m["content"]) for m in data["messages"]) < 8192 - data["max_tokens"]
        assert data["messages"][-1]["content"] == "Продолжим"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"intent":"conversation","answer":"Продолжим."}'}}]})

    models = LocalModels()
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": "длинная реплика " * 130} for i in range(32)]
    try:
        result = await models.extract("Продолжим", history, {}, [], [], "2026-10-13")
        assert result.answer == "Продолжим."
        assert sum(path.endswith("/tokenize") for path in calls) <= 8
    finally:
        await models.close()


async def test_short_compact_request_omits_tokenizer_round_trips_and_disables_thinking(monkeypatch):
    monkeypatch.setattr(settings, "llm_prompt_profile", "compact")
    monkeypatch.setattr(settings, "llm_thinking", False)
    observed = []

    async def handler(request):
        observed.append(request.url.path)
        assert request.url.path.endswith("/chat/completions")
        data = json.loads(request.content)
        assert data["chat_template_kwargs"] == {"enable_thinking": False}
        assert "ПОСЛЕДНЕЙ" in data["messages"][0]["content"]
        assert "Алексей" in data["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"intent":"conversation","answer":"Назовите основной вопрос."}'}}]})

    models = LocalModels()
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await models.extract("Как подготовиться?", [], {"customer_memory": {"customer_name": "Алексей"}}, [], [], "2026-10-13")
        assert result.answer == "Назовите основной вопрос."
        assert len(observed) == 1
    finally:
        await models.close()


async def test_current_question_is_never_trimmed_to_fit_context():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        data = json.loads(request.content)
        if request.url.path.endswith("/apply-template"):
            return httpx.Response(200, json={"prompt": "\n".join(m["content"] for m in data["messages"])})
        if request.url.path.endswith("/tokenize"):
            return httpx.Response(200, json={"tokens": list(range(len(data["content"])))})
        raise AssertionError("Oversized question must be rejected before inference")

    models = LocalModels()
    await models.http.aclose()
    models.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AppError) as error:
            await models.extract("Большой вопрос " * 1000, [], {}, [], [], "2026-10-13")
        assert error.value.code == "llm_context_overflow"
        assert not any(path.endswith("/chat/completions") for path in calls)
    finally:
        await models.close()
