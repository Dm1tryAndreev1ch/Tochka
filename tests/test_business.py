import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.business import MINSK
from app.errors import AppError
from app.schemas import BookingDraft, CallbackDraft, ManagementChange


async def available_slot(business, service="consultation"):
    for offset in range(1, 8):
        slots = await business.slots(service, datetime.now(MINSK).date() + timedelta(days=offset))
        if slots:
            return slots[0]["starts_at"]
    raise AssertionError("No slots")


async def draft(business, sid, when=None):
    when = when or await available_slot(business)
    return await business.draft(
        sid,
        "booking",
        BookingDraft(
            service_id="consultation", starts_at=when, name="Тестовый клиент", phone="+375291234567"
        ),
    )


async def test_booking_needs_confirmation(business, session):
    data = await draft(business, session["uuid"])
    assert data["status"] == "awaiting_confirmation"
    assert await business.pool.fetchval("SELECT count(*) FROM bookings") == 0
    result = await business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4())
    assert result["status"] == "succeeded"
    assert result["crm_status"] == "pending_sync"
    assert await business.pool.fetchval("SELECT count(*) FROM bookings") == 1
    assert await business.pool.fetchval("SELECT count(*) FROM outbox") == 3


async def test_ten_retries_one_booking(business, session):
    data = await draft(business, session["uuid"])
    key = uuid4()
    results = await asyncio.gather(
        *(business.confirm(session["uuid"], UUID(data["id"]), data["revision"], key) for _ in range(10))
    )
    assert len({r["action_id"] for r in results}) == 1
    assert len({r["management_token"] for r in results}) == 1
    assert await business.pool.fetchval("SELECT count(*) FROM bookings") == 1
    assert await business.pool.fetchval("SELECT count(*) FROM requests") == 1


async def test_distinct_keys_same_draft_one_booking(business, session):
    data = await draft(business, session["uuid"])
    results = await asyncio.gather(
        *(business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4()) for _ in range(4))
    )
    assert len({r["action_id"] for r in results}) == 1


async def test_racing_clients_only_one_gets_slot(business, session):
    other = await business.new_session("chat", True)
    when = await available_slot(business)
    a, b = await asyncio.gather(
        draft(business, session["uuid"], when), draft(business, UUID(other["id"]), when)
    )
    results = await asyncio.gather(
        business.confirm(session["uuid"], UUID(a["id"]), a["revision"], uuid4()),
        business.confirm(UUID(other["id"]), UUID(b["id"]), b["revision"], uuid4()),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert next(r for r in results if isinstance(r, AppError)).code == "slot_taken"
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 1


async def test_overlap_different_start_times_rejected(business, session):
    when = await available_slot(business)
    data = await draft(business, session["uuid"], when)
    await business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4())
    other = await business.new_session("chat", True)
    overlap = (datetime.fromisoformat(when) + timedelta(minutes=15)).isoformat()
    with pytest.raises(AppError, match="занято"):
        await draft(business, UUID(other["id"]), overlap)


async def test_correction_revokes_old_confirmation(business, session):
    a = await draft(business, session["uuid"])
    b = await draft(
        business,
        session["uuid"],
        (datetime.fromisoformat(a["payload"]["starts_at"]) + timedelta(hours=1)).isoformat(),
    )
    assert b["revision"] > a["revision"]
    with pytest.raises(AppError) as error:
        await business.confirm(session["uuid"], UUID(a["id"]), a["revision"], uuid4())
    assert error.value.code == "stale_confirmation"
    assert await business.pool.fetchval("SELECT count(*) FROM bookings") == 0


async def test_service_price_change_requires_new_summary(business, session):
    data = await draft(business, session["uuid"])
    await business.pool.execute("UPDATE services SET price=100,revision=revision+1 WHERE id='consultation'")
    with pytest.raises(AppError) as error:
        await business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4())
    assert error.value.code == "service_changed"


async def test_wrong_session_cannot_confirm(business, session):
    data = await draft(business, session["uuid"])
    other = await business.new_session("chat", True)
    with pytest.raises(AppError) as error:
        await business.confirm(UUID(other["id"]), UUID(data["id"]), data["revision"], uuid4())
    assert error.value.status == 404


async def test_expired_session_cannot_commit(business, session):
    data = await draft(business, session["uuid"])
    await business.pool.execute(
        "UPDATE sessions SET expires_at=now()-interval '1 second' WHERE id=$1", session["uuid"]
    )
    with pytest.raises(AppError) as error:
        await business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4())
    assert error.value.status == 401


async def test_management_requires_secret_and_confirmation(business, session):
    data = await draft(business, session["uuid"])
    result = await business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4())
    with pytest.raises(AppError):
        await business.management("+375291234567")
    booking = await business.management(result["management_token"])
    preview = await business.change_booking(
        ManagementChange(token=result["management_token"], operation="cancel", revision=booking["revision"])
    )
    assert preview["status"] == "needs_confirmation"
    assert (await business.management(result["management_token"]))["status"] == "confirmed"
    await business.change_booking(
        ManagementChange(
            token=result["management_token"], operation="cancel", revision=booking["revision"], confirm=True
        )
    )
    assert (await business.management(result["management_token"]))["status"] == "cancelled"
    assert await business.pool.fetchval("SELECT count(*) FROM outbox WHERE status='pending'") == 3


async def test_expired_management_token_denied(business, session):
    data = await draft(business, session["uuid"])
    result = await business.confirm(session["uuid"], UUID(data["id"]), data["revision"], uuid4())
    await business.pool.execute("UPDATE bookings SET token_expires_at=now()-interval '1 second'")
    with pytest.raises(AppError):
        await business.management(result["management_token"])


async def test_unpublished_knowledge_is_not_used(business):
    kid = uuid4()
    await business.pool.execute(
        "INSERT INTO knowledge(id,title,content,source) VALUES($1,'Черновик','Непроверенный факт','Тест')",
        kid,
    )
    assert await business.knowledge() == []
    await business.pool.execute(
        "UPDATE knowledge SET published_title=title,published_content=content,published_revision=revision WHERE id=$1",
        kid,
    )
    await business.pool.execute(
        "UPDATE knowledge SET content='Изменённый черновик',revision=revision+1 WHERE id=$1", kid
    )
    assert (await business.knowledge())[0]["content"] == "Непроверенный факт"
    await business.pool.execute("UPDATE knowledge SET archived=true WHERE id=$1", kid)
    assert await business.knowledge() == []


async def test_no_consent_no_session(business):
    with pytest.raises(AppError):
        await business.new_session("chat", False)
    assert await business.pool.fetchval("SELECT count(*) FROM sessions") == 0


async def test_e164_normalization():
    assert (
        CallbackDraft(name="Тест", phone="+375 (29) 123-45-67", message="Позвоните").phone == "+375291234567"
    )
    with pytest.raises(ValueError):
        CallbackDraft(name="Тест", phone="0291234567", message="Позвоните")
