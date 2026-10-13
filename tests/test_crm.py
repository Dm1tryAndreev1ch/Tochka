from uuid import UUID, uuid4

import httpx

from app.crm import CRMWorker
from app.schemas import CallbackDraft


async def queued(business, session):
    draft = await business.draft(
        session["uuid"], "callback", CallbackDraft(name="Тест", phone="+375291234567", message="Перезвоните")
    )
    return await business.confirm(session["uuid"], UUID(draft["id"]), draft["revision"], uuid4())


async def test_crm_recovers_unknown_post_outcome_without_duplicate(business, session):
    await queued(business, session)
    stored = {}
    posts = []
    fail_once = True

    async def handler(request):
        nonlocal fail_once
        entity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            items = [stored[entity]] if entity in stored else []
            return httpx.Response(200, json={"list": items})
        if request.method == "POST":
            assert request.headers["X-Skip-Duplicate-Check"] == "true"
            posts.append(entity)
            stored[entity] = {"id": entity + "_id"}
            if fail_once:
                fail_once = False
                raise httpx.ReadTimeout("Response lost after successful CRM commit", request=request)
            return httpx.Response(200, json=stored[entity])
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    worker = CRMWorker(business.pool)
    await worker.http.aclose()
    worker.http = httpx.AsyncClient(base_url="http://crm/api/v1/", transport=httpx.MockTransport(handler))
    try:
        assert await worker.sync_one()
        assert posts == ["Lead"]
        # Call and Task wait for a confirmed CRM Lead mapping.
        assert not await worker.sync_one()
        await business.pool.execute("UPDATE outbox SET next_attempt_at=now()")
        for _ in range(4):
            await worker.sync_one()
        assert sorted(posts) == ["Call", "Lead", "Task"]
        assert await business.pool.fetchval("SELECT count(*) FROM outbox WHERE status='synced'") == 3
    finally:
        await worker.close()


async def test_crm_outage_preserves_request_and_pending_status(business, session):
    await queued(business, session)

    def handler(request):
        return httpx.Response(503)

    worker = CRMWorker(business.pool)
    await worker.http.aclose()
    worker.http = httpx.AsyncClient(base_url="http://crm/api/v1/", transport=httpx.MockTransport(handler))
    try:
        assert await worker.sync_one()
        assert await business.pool.fetchval("SELECT count(*) FROM requests") == 1
        assert await business.pool.fetchval("SELECT count(*) FROM outbox WHERE status='synced'") == 0
        assert await business.pool.fetchval("SELECT count(*) FROM outbox WHERE error IS NOT NULL") == 1
    finally:
        await worker.close()
