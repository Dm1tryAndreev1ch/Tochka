from datetime import UTC, datetime
from uuid import UUID

from app.config import settings


async def test_login_role_is_derived_from_individual_account(client):
    response = await client.post("/api/v1/operator/login", json={"username": "operator", "role": "admin", "password": settings.operator_password})
    assert response.status_code == 200
    assert response.json()["role"] == "operator"
    assert response.json()["user_id"]


async def test_operator_cannot_read_archived_transcript_without_permission(client, business):
    session = await business.new_session("chat", True)
    await business.pool.execute("UPDATE sessions SET ended_at=$2,state='ended' WHERE id=$1", UUID(session["id"]), datetime.now(UTC))
    await client.post("/api/v1/operator/login", json={"role": "operator", "password": settings.operator_password})
    response = await client.get("/api/v1/operator/sessions/" + session["id"])
    assert response.status_code == 403
