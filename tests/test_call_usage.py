from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.config import settings


async def test_dashboard_counts_connected_calls_and_abandoned_wait(client, business):
    now = datetime.now(UTC)
    answered = UUID((await business.new_session("voice", False))["id"])
    abandoned = UUID((await business.new_session("voice", False))["id"])
    await business.new_session("voice", False)  # Created, never connected.
    await business.pool.execute(
        "UPDATE sessions SET connected_at=$2,started_at=$3,ended_at=$4,state='ended' WHERE id=$1",
        answered, now - timedelta(seconds=30), now - timedelta(seconds=20), now,
    )
    await business.pool.execute(
        "UPDATE sessions SET connected_at=$2,ended_at=$3,state='ended' WHERE id=$1",
        abandoned, now - timedelta(seconds=5), now,
    )
    await business.pool.execute(
        "UPDATE session_usage SET llm_requests=2,prompt_tokens=120,completion_tokens=30,unreported_requests=1 WHERE session_id=$1", answered,
    )
    await client.post("/api/v1/operator/login", json={"role": "admin", "password": settings.admin_password})
    response = await client.get("/api/v1/operator/usage")
    assert response.status_code == 200
    data = response.json()
    assert data["totals"]["calls"] == 2
    assert data["totals"]["abandoned_calls"] == 1
    assert float(data["totals"]["talk_seconds"]) == pytest.approx(20)
    assert float(data["totals"]["wait_seconds"]) == pytest.approx(15)
    assert float(data["totals"]["connected_seconds"]) == pytest.approx(35)
    assert data["totals"]["prompt_tokens"] == 120
    assert data["totals"]["completion_tokens"] == 30
    assert data["totals"]["unreported_requests"] == 1
    assert len(data["recent"]) == 2
    rows = {r["id"]: r for r in data["recent"]}
    assert float(rows[str(abandoned)]["wait_seconds"]) == pytest.approx(5)
    assert float(rows[str(abandoned)]["talk_seconds"]) == 0
    assert float(data["daily"][0]["wait_seconds"]) == pytest.approx(15)
