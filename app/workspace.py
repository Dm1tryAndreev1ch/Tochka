"""Company settings, staff, agents and aggregate operational dashboards."""

import asyncio
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app import db
from app.access import ALL_PERMISSIONS, ROLE_PERMISSIONS, password_hash
from app.business import json_record
from app.errors import AppError


class CompanyUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    record_audio: bool = False
    text_days: int = Field(ge=1, le=365)
    audio_days: int = Field(ge=1, le=180)


class StaffCreate(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{2,60}$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=16, max_length=200)
    role: Literal["admin", "operator", "auditor", "technical"]
    permissions: list[str] | None = Field(default=None, max_length=30)


class StaffUpdate(BaseModel):
    active: bool
    revision: int = Field(ge=1)
    permissions: list[str] = Field(max_length=30)
    password: str | None = Field(default=None, min_length=16, max_length=200)


class AgentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    instructions: str = Field(default="", max_length=2500)
    knowledge_ids: list[UUID] = Field(default_factory=list, max_length=100)
    allowed_actions: list[Literal["booking", "callback", "handoff"]] = Field(
        default_factory=lambda: ["booking", "callback", "handoff"], max_length=3
    )
    revision: int = Field(ge=1)


def workspace_router(operator):
    router = APIRouter(prefix="/api/v1/operator")

    @router.get("/company")
    async def company(request: Request, role=Depends(operator)):
        row = await request.app.state.pool.fetchrow("SELECT * FROM company WHERE id=1")
        return {**json_record(row), "single_company": True}

    @router.put("/company")
    async def set_company(data: CompanyUpdate, request: Request, role=Depends(operator)):
        pool = request.app.state.pool
        row = await pool.fetchrow("SELECT * FROM company WHERE id=1")
        policy = {
            "record_audio": data.record_audio,
            "text_days": data.text_days,
            "audio_days": data.audio_days,
            "policy_version": row["settings"].get("policy_version", 1) + 1,
        }
        updated = await pool.fetchrow(
            "UPDATE company SET name=$1,settings=$2,revision=revision+1 WHERE id=1 AND revision=$3 RETURNING *",
            data.name.strip(),
            policy,
            data.revision,
        )
        if not updated:
            raise AppError("revision_conflict", "Настройки уже изменены. Обновите страницу", 409)
        await db.audit(
            pool,
            role,
            "company_updated",
            "1",
            {"revision": updated["revision"], "policy_version": policy["policy_version"]},
        )
        return json_record(updated)

    @router.get("/staff")
    async def staff(request: Request, role=Depends(operator)):
        rows = await request.app.state.pool.fetch(
            "SELECT id,username,display_name,role,permissions,active,revision,created_at FROM staff_users ORDER BY created_at LIMIT 100"
        )
        return {"items": [json_record(r) for r in rows], "permissions": sorted(ALL_PERMISSIONS)}

    @router.post("/staff", status_code=201)
    async def create_staff(data: StaffCreate, request: Request, role=Depends(operator)):
        permissions = (
            ROLE_PERMISSIONS[data.role]
            if data.permissions is None or data.role == "admin"
            else set(data.permissions)
        )
        if not set(permissions) <= ALL_PERMISSIONS:
            raise AppError("invalid_permission", "Неизвестное разрешение", 422)
        pool = request.app.state.pool
        if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM staff_users WHERE username=$1)", data.username):
            raise AppError("username_exists", "Такое имя входа уже занято", 409)
        uid = uuid4()
        encoded = await asyncio.to_thread(password_hash, data.password)
        await pool.execute(
            "INSERT INTO staff_users(id,username,display_name,password_hash,role,permissions) VALUES($1,$2,$3,$4,$5,$6)",
            uid,
            data.username,
            data.display_name,
            encoded,
            data.role,
            sorted(permissions),
        )
        await db.audit(
            pool, role, "staff_created", uid, {"role": data.role, "permissions": sorted(permissions)}
        )
        return {"id": str(uid)}

    @router.put("/staff/{uid}")
    async def update_staff(uid: UUID, data: StaffUpdate, request: Request, role=Depends(operator)):
        pool = request.app.state.pool
        row = await pool.fetchrow("SELECT * FROM staff_users WHERE id=$1 FOR UPDATE", uid)
        if not row:
            raise AppError("not_found", "Учётная запись не найдена", 404)
        if not set(data.permissions) <= ALL_PERMISSIONS:
            raise AppError("invalid_permission", "Неизвестное разрешение", 422)
        if (
            row["role"] == "admin"
            and not data.active
            and await pool.fetchval("SELECT count(*) FROM staff_users WHERE role='admin' AND active") <= 1
        ):
            raise AppError("last_admin", "Нельзя отключить последнего владельца", 409)
        permissions = sorted(ALL_PERMISSIONS if row["role"] == "admin" else set(data.permissions))
        encoded = (
            await asyncio.to_thread(password_hash, data.password) if data.password else row["password_hash"]
        )
        updated = await pool.fetchrow(
            "UPDATE staff_users SET active=$2,permissions=$3,password_hash=$4,revision=revision+1 WHERE id=$1 AND revision=$5 RETURNING id,revision",
            uid,
            data.active,
            permissions,
            encoded,
            data.revision,
        )
        if not updated:
            raise AppError("revision_conflict", "Учётная запись уже изменена", 409)
        await pool.execute("DELETE FROM admin_sessions WHERE user_id=$1", uid)
        await db.audit(
            pool,
            role,
            "staff_updated",
            uid,
            {"active": data.active, "permissions": permissions, "password_changed": bool(data.password)},
        )
        voice = request.app.state.voice
        if hasattr(voice, "revoke_staff"):
            await voice.revoke_staff(user_id=uid)
        return json_record(updated)

    @router.get("/agents")
    async def agents(request: Request, role=Depends(operator)):
        rows = await request.app.state.pool.fetch("SELECT * FROM agents ORDER BY profile")
        return {"items": [json_record(r) for r in rows]}

    @router.put("/agents/{profile}")
    async def update_agent(
        profile: Literal["male", "female"], data: AgentUpdate, request: Request, role=Depends(operator)
    ):
        pool = request.app.state.pool
        if data.knowledge_ids:
            count = await pool.fetchval(
                "SELECT count(*) FROM knowledge WHERE id=ANY($1::uuid[]) AND NOT archived", data.knowledge_ids
            )
            if count != len(set(data.knowledge_ids)):
                raise AppError("invalid_knowledge", "Один из документов недоступен", 422)
        row = await pool.fetchrow(
            "UPDATE agents SET name=$2,instructions=$3,knowledge_ids=$4,allowed_actions=$5,revision=revision+1,updated_at=now() WHERE profile=$1 AND revision=$6 RETURNING *",
            profile,
            data.name.strip(),
            data.instructions,
            [str(v) for v in data.knowledge_ids],
            sorted(set(data.allowed_actions)),
            data.revision,
        )
        if not row:
            raise AppError("revision_conflict", "Настройки агента уже изменены", 409)
        await db.audit(
            pool,
            role,
            "agent_updated",
            profile,
            {
                "revision": row["revision"],
                "knowledge_ids": row["knowledge_ids"],
                "allowed_actions": row["allowed_actions"],
            },
        )
        return json_record(row)

    @router.get("/usage")
    async def usage(request: Request, days: int = Query(default=7, ge=1, le=90), role=Depends(operator)):
        pool = request.app.state.pool
        calls = """WITH calls AS (
            SELECT s.*,coalesce(u.prompt_tokens,0) AS prompt_tokens,
                coalesce(u.completion_tokens,0) AS completion_tokens,
                coalesce(u.llm_requests,0) AS llm_requests,
                coalesce(u.unreported_requests,0) AS unreported_requests,
                greatest(0,extract(epoch FROM(coalesce(s.ended_at,now())-s.connected_at))) AS connected_seconds,
                CASE WHEN s.started_at IS NULL THEN 0
                    ELSE greatest(0,extract(epoch FROM(coalesce(s.ended_at,now())-s.started_at))) END AS talk_seconds,
                greatest(0,extract(epoch FROM(coalesce(s.started_at,s.ended_at,now())-s.connected_at))) AS wait_seconds
            FROM sessions s LEFT JOIN session_usage u ON u.session_id=s.id
            WHERE s.channel='voice' AND s.connected_at>now()-$1::int*interval '1 day'
        ) """
        totals = await pool.fetchrow(
            calls + """SELECT count(*) AS calls,
                count(*) FILTER(WHERE ended_at IS NOT NULL AND started_at IS NULL) AS abandoned_calls,
                count(*) FILTER(WHERE ended_at IS NULL AND started_at IS NULL) AS waiting_calls,
                coalesce(sum(connected_seconds),0) AS connected_seconds,
                coalesce(sum(talk_seconds),0) AS talk_seconds,
                coalesce(sum(wait_seconds),0) AS wait_seconds,
                coalesce(sum(prompt_tokens),0) AS prompt_tokens,
                coalesce(sum(completion_tokens),0) AS completion_tokens,
                coalesce(sum(llm_requests),0) AS llm_requests,
                coalesce(sum(unreported_requests),0) AS unreported_requests FROM calls""",
            days,
        )
        daily = await pool.fetch(
            calls + """SELECT (connected_at AT TIME ZONE 'Europe/Minsk')::date AS day,
                count(*) AS calls,sum(prompt_tokens+completion_tokens) AS tokens,
                sum(talk_seconds) AS talk_seconds,sum(wait_seconds) AS wait_seconds
                FROM calls GROUP BY day ORDER BY day""",
            days,
        )
        recent = await pool.fetch(
            calls + """SELECT id,source,voice_profile,state,created_at,connected_at,started_at,ended_at,
                connected_seconds,talk_seconds,wait_seconds,prompt_tokens,completion_tokens,
                llm_requests,unreported_requests FROM calls ORDER BY connected_at DESC LIMIT 30""",
            days,
        )
        return {
            "totals": json_record(totals),
            "daily": [json_record(r) for r in daily],
            "recent": [json_record(r) for r in recent],
            "token_scope": "LLM only; unreported requests are counted separately",
            "time_scope": "Connection time = queue wait + time with the assistant or employee, including pauses and disclosure",
        }

    @router.get("/crm-summary")
    async def crm_summary(
        request: Request, days: int = Query(default=7, ge=1, le=90), role=Depends(operator)
    ):
        pool = request.app.state.pool
        counts = await pool.fetch("SELECT status,count(*) AS count FROM outbox GROUP BY status")
        daily = await pool.fetch(
            "SELECT (coalesce(synced_at,last_attempt_at,created_at) AT TIME ZONE 'Europe/Minsk')::date AS day,status,count(*) AS count FROM outbox WHERE coalesce(synced_at,last_attempt_at,created_at)>now()-$1::int*interval '1 day' GROUP BY day,status ORDER BY day",
            days,
        )
        recent = await pool.fetch(
            "SELECT o.id,o.action_id,o.entity,o.status,o.attempts,o.error,o.external_id,o.created_at,o.last_attempt_at,o.synced_at,r.kind FROM outbox o LEFT JOIN requests r ON r.action_id=o.action_id ORDER BY coalesce(o.last_attempt_at,o.created_at) DESC LIMIT 20"
        )
        return {
            "counts": [json_record(r) for r in counts],
            "daily": [json_record(r) for r in daily],
            "recent": [json_record(r) for r in recent],
        }

    return router
