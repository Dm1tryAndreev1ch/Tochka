import json
from uuid import UUID, uuid4

from app.privacy import enforce_retention, erase_request
from app.schemas import CallbackDraft


async def test_erasure_covers_all_persistent_copies(business, session):
    sid = session["uuid"]
    draft = await business.draft(
        sid, "callback", CallbackDraft(name="Секретное имя", phone="+375291234567", message="Личный вопрос")
    )
    result = await business.confirm(sid, UUID(draft["id"]), draft["revision"], uuid4())
    await business.pool.execute(
        "INSERT INTO turns(session_id,role,text,reply) VALUES($1,'user','Личный вопрос',$2)",
        sid,
        {"name": "Секретное имя"},
    )
    await erase_request(business.pool, UUID(result["request_id"]))
    for table in ["sessions", "turns", "drafts", "actions", "requests", "outbox"]:
        rows = await business.pool.fetch("SELECT row_to_json(t) AS data FROM " + table + " t")
        text = json.dumps([r["data"] for r in rows], ensure_ascii=False)
        assert "Секретное имя" not in text
        assert "+375291234567" not in text
        assert "Личный вопрос" not in text
    assert await business.pool.fetchval("SELECT count(*) FROM outbox WHERE status='pending'") == 3


async def test_retention_anonymizes_actions_and_outbox_too(business, session):
    draft = await business.draft(
        session["uuid"],
        "callback",
        CallbackDraft(name="Старое имя", phone="+375291234567", message="Истёкший вопрос"),
    )
    await business.confirm(session["uuid"], UUID(draft["id"]), draft["revision"], uuid4())
    await business.pool.execute("UPDATE requests SET created_at=now()-interval '31 days'")
    await enforce_retention(business.pool, 30)
    assert await business.pool.fetchval("SELECT phone FROM requests LIMIT 1") == ""
    assert await business.pool.fetchval("SELECT result->>'deleted' FROM actions LIMIT 1") == "true"
    assert "37529" not in json.dumps(
        await business.pool.fetchval("SELECT payload FROM outbox WHERE entity='Lead'")
    )
