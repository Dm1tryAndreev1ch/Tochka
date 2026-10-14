from uuid import UUID, uuid4

import pytest

from app.schemas import CallbackDraft


async def test_callback_api_cannot_store_customer_data_before_consent(client, business):
    call = await business.new_session("voice", False)
    sid = UUID(call["id"])
    response = await client.post(
        f"/api/v1/sessions/{sid}/drafts/callbacks",
        headers={"Authorization": "Bearer " + call["token"]},
        json={"name": "Тестовый клиент", "phone": "+375291234567", "message": "Перезвоните"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consent_required"
    row = await business.pool.fetchrow("SELECT context,memory FROM sessions WHERE id=$1", sid)
    assert row["context"] == {} and row["memory"] == {}
    for table in ("drafts", "requests", "actions", "outbox"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_confirmation_api_rechecks_consent_before_committing(client, business, session):
    draft = await business.draft(
        session["uuid"], "callback",
        CallbackDraft(name="Тестовый клиент", phone="+375291234567", message="Перезвоните"),
    )
    await business.pool.execute("UPDATE sessions SET consent=false WHERE id=$1", session["uuid"])
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/drafts/{draft['id']}/confirmations",
        headers={"Authorization": "Bearer " + session["token"]},
        json={"revision": draft["revision"], "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consent_required"
    for table in ("requests", "actions", "outbox"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_confirmation_rechecks_allowed_actions(client, business, session):
    draft = await business.draft(
        session["uuid"], "callback",
        CallbackDraft(name="Тестовый клиент", phone="+375291234567", message="Перезвоните"),
    )
    await business.pool.execute(
        "UPDATE sessions SET agent_config=jsonb_set(agent_config,'{allowed_actions}','[]') WHERE id=$1",
        session["uuid"],
    )
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/drafts/{draft['id']}/confirmations",
        headers={"Authorization": "Bearer " + session["token"]},
        json={"revision": draft["revision"], "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "action_disabled"
    for table in ("requests", "actions", "outbox"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("state,code", [("queued", "call_queued"), ("human", "human_active")])
@pytest.mark.parametrize("operation", ["draft", "confirm"])
async def test_customer_cannot_mutate_actions_while_waiting_or_with_staff(
    client, business, session, state, code, operation,
):
    data = {"name": "Тестовый клиент", "phone": "+375291234567", "message": "Перезвоните"}
    sid = session["uuid"]
    if operation == "confirm":
        draft = await business.draft(sid, "callback", CallbackDraft(**data))
        path = f"/api/v1/sessions/{sid}/drafts/{draft['id']}/confirmations"
        payload = {"revision": draft["revision"], "idempotency_key": str(uuid4())}
    else:
        path, payload = f"/api/v1/sessions/{sid}/drafts/callbacks", data
    await business.pool.execute("UPDATE sessions SET state=$2 WHERE id=$1", sid, state)
    response = await client.post(
        path, headers={"Authorization": "Bearer " + session["token"]}, json=payload,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code
    for table in ("requests", "actions", "outbox"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert await business.pool.fetchval("SELECT count(*) FROM drafts") == (operation == "confirm")


async def test_handoff_api_obeys_agent_allowed_actions(client, business, session):
    await business.pool.execute("UPDATE settings SET value='{\"available\":true}' WHERE key='operator'")
    await business.pool.execute(
        "UPDATE sessions SET agent_config=jsonb_set(agent_config,'{allowed_actions}','[]') WHERE id=$1",
        session["uuid"],
    )
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/handoffs",
        headers={"Authorization": "Bearer " + session["token"]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "action_disabled"
    assert await business.pool.fetchval("SELECT state FROM sessions WHERE id=$1", session["uuid"]) == "bot"
