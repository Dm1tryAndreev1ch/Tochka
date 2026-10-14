import asyncio
from types import SimpleNamespace

from app.main import app, housekeeping


async def test_readiness_reports_database_outage_and_recovers(client, monkeypatch):
    healthy_pool = app.state.pool

    class Outage:
        async def fetchval(self, *args):
            raise ConnectionError("test outage")

    monkeypatch.setattr(app.state, "pool", Outage())
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] is False
    assert response.json()["voice_available"] is False
    monkeypatch.setattr(app.state, "pool", healthy_pool)
    recovered = await client.get("/api/v1/health/ready")
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "ready"


async def test_housekeeping_continues_after_temporary_database_outage():
    recovered = asyncio.Event()

    class Pool:
        attempts = 0

        async def fetch(self, query, *args):
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("temporary outage")
            if "UPDATE sessions" in query:
                recovered.set()
            return []

        async def execute(self, *args):
            pass

    state = SimpleNamespace(pool=Pool(), socket_calls=None)
    worker = asyncio.create_task(housekeeping(SimpleNamespace(state=state), interval=0.01))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
