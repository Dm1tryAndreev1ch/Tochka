from uuid import uuid4

from app.config import settings


async def customer(client):
    response = await client.post("/api/v1/sessions", json={"channel": "chat", "consent": True})
    assert response.status_code == 201
    data = response.json()
    return data, {"Authorization": "Bearer " + data["token"]}


async def test_session_auth_and_isolation(client):
    a, ah = await customer(client)
    b, bh = await customer(client)
    assert (await client.get("/api/v1/sessions/" + a["id"])).status_code == 401
    assert (await client.get("/api/v1/sessions/" + a["id"], headers=bh)).status_code == 401
    assert (await client.get("/api/v1/sessions/" + a["id"], headers=ah)).status_code == 200


async def test_operator_login_and_permissions(client):
    assert (await client.get("/api/v1/operator/requests")).status_code == 401
    assert (await client.post("/api/v1/operator/login", json={"password": "bad"})).status_code == 401
    response = await client.post(
        "/api/v1/operator/login", json={"password": settings.operator_password, "role": "operator"}
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert (await client.get("/api/v1/operator/overview")).status_code == 200
    assert (
        await client.post(
            "/api/v1/operator/knowledge", json={"title": "Тест", "content": "Достоверные факты"}
        )
    ).status_code == 403


async def test_cross_origin_and_host_rejected(client):
    response = await client.post(
        "/api/v1/sessions",
        headers={"Origin": "https://evil.example"},
        json={"channel": "chat", "consent": True},
    )
    assert response.status_code == 403
    assert (await client.get("/api/v1/services", headers={"Host": "evil.example"})).status_code == 400


async def test_message_retries_and_key_conflict(client):
    session, headers = await customer(client)
    key = str(uuid4())
    path = "/api/v1/sessions/" + session["id"] + "/messages"
    payload = {"text": "Какие услуги у вас есть?", "idempotency_key": key}
    a = await client.post(path, headers=headers, json=payload)
    b = await client.post(path, headers=headers, json=payload)
    assert a.status_code == 200 and a.json() == b.json()
    assert (
        await client.post(path, headers=headers, json={**payload, "text": "Другой вопрос"})
    ).status_code == 409
    detail = (await client.get("/api/v1/sessions/" + session["id"], headers=headers)).json()
    assert len(detail["turns"]) == 2


async def test_invalid_phone_cannot_prepare_callback(client):
    session, headers = await customer(client)
    result = await client.post(
        "/api/v1/sessions/" + session["id"] + "/drafts/callbacks",
        headers=headers,
        json={"name": "Тест", "phone": "123", "message": "Перезвоните"},
    )
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "validation_error"


async def test_public_api_never_exposes_contacts(client):
    assert (await client.get("/api/v1/operator/bookings?day=2026-10-13")).status_code == 401
    response = await client.get("/api/v1/services")
    assert response.status_code == 200
    assert all("phone" not in service for service in response.json()["items"])


async def test_admin_publication_workflow(client):
    await client.post("/api/v1/operator/login", json={"password": settings.admin_password})
    create = await client.post(
        "/api/v1/operator/knowledge", json={"title": "Тест", "content": "Проверенный адрес", "source": "Тест"}
    )
    assert create.status_code == 201
    doc = create.json()
    publish = await client.post(
        "/api/v1/operator/knowledge/" + doc["id"] + "/publications", json={"revision": doc["revision"]}
    )
    assert publish.status_code == 200
    stale = await client.put(
        "/api/v1/operator/knowledge/" + doc["id"] + "?revision=10",
        json={"title": "Тест", "content": "Другие данные"},
    )
    assert stale.status_code == 409


async def test_end_session_revokes_access(client):
    session, headers = await customer(client)
    path = "/api/v1/sessions/" + session["id"]
    assert (await client.delete(path, headers=headers)).status_code == 204
    assert (await client.get(path, headers=headers)).status_code == 401
