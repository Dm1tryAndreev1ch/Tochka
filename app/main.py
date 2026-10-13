import asyncio
import contextlib
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import Cookie, Depends, FastAPI, Form, Header, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from app import db
from app.business import Business, json_record, token_hash
from app.call_modes import CallModes
from app.config import ROOT, settings
from app.dialogue import Dialogue, EventBus
from app.errors import AppError
from app.models import LocalModels
from app.schemas import (
    Availability,
    BookingDraft,
    CallbackDraft,
    CallModeInput,
    Confirmation,
    KnowledgeInput,
    KnowledgePublish,
    Login,
    ManagementChange,
    Message,
    Offer,
    OperatorReply,
    ScheduleInput,
    ServiceInput,
    SessionCreate,
    StatusInput,
    TelegramAuthorization,
    TelegramCode,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("voice")


async def housekeeping(app):
    while True:
        await asyncio.sleep(60)
        expired = await app.state.pool.fetch(
            "UPDATE sessions SET ended_at=now(),state='ended' WHERE expires_at<now() AND ended_at IS NULL RETURNING id"
        )
        for row in expired:
            await app.state.voice.end(row["id"])
            app.state.dialogue.release(row["id"])
            await app.state.events.emit(row["id"], {"type": "ended"})
        await app.state.pool.execute("DELETE FROM admin_sessions WHERE expires_at<now()")
        from app.privacy import enforce_retention

        await enforce_retention(app.state.pool, settings.retention_days)
        if getattr(app.state, "socket_calls", None):
            await app.state.socket_calls.purge()


@asynccontextmanager
async def lifespan(app):
    if len(settings.admin_password) < 16 or len(settings.management_secret) < 32:
        raise RuntimeError("Run scripts/setup.py: strong admin password and management secret are required")
    from app.crm import CRMWorker
    from app.voice import VoiceManager

    app.state.pool = await db.connect(settings.database_url)
    app.state.events = EventBus()
    app.state.business = Business(app.state.pool)
    app.state.models = LocalModels()
    app.state.dialogue = Dialogue(app.state.business, app.state.models, app.state.events)
    app.state.voice = VoiceManager(app.state.business, app.state.models, app.state.dialogue, app.state.events)
    app.state.crm = CRMWorker(app.state.pool)
    app.state.socket_calls = None
    if settings.socket_transport_enabled and len(settings.channel_api_key) >= 16:
        from app.socket_transport import SocketCalls

        app.state.socket_calls = SocketCalls(app.state.business, app.state.voice, app.state.models)
        await app.state.socket_calls.start()
    tasks = [
        asyncio.create_task(asyncio.to_thread(app.state.models.load)),
        asyncio.create_task(app.state.crm.run()),
        asyncio.create_task(housekeeping(app)),
    ]
    # Seed published test facts once; never overwrite the owner's edits.
    async with app.state.pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM knowledge)"):
            await conn.execute(
                "INSERT INTO knowledge(id,title,content,source,published_revision,published_content,published_title) VALUES($1,$2,$3,$4,1,$3,$2)",
                uuid4(),
                "О тестовой компании",
                "Точка — вымышленная компания для проверки приложения. Адрес для тестов: Минск, улица Примерная, 10. Оплата на месте, белорусские рубли. Услуги: консультация, диагностика и настройка. Рабочие часы задаются в расписании. Не выдавайте эти данные за сведения реального бизнеса.",
                "Тестовые данные",
            )
    yield
    if app.state.socket_calls:
        await app.state.socket_calls.close()
    await app.state.voice.close()
    app.state.crm.stop.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.state.crm.close()
    await app.state.models.close()
    await app.state.pool.close()


app = FastAPI(title="Точка · Voice Desk", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts.split(","))
rate_buckets = defaultdict(deque)


def limit(key, maximum=30, seconds=60):
    now = time.monotonic()
    bucket = rate_buckets[key]
    while bucket and bucket[0] < now - seconds:
        bucket.popleft()
    if len(bucket) >= maximum:
        raise AppError("rate_limited", "Слишком много запросов. Повторите через минуту", 429)
    bucket.append(now)
    if len(rate_buckets) > 2048:
        for old in list(rate_buckets):
            if not rate_buckets[old] or rate_buckets[old][-1] < now - 300:
                rate_buckets.pop(old, None)


@app.middleware("http")
async def security(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    origin = request.headers.get("origin")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and origin
        and origin not in settings.allowed_origins.split(",")
    ):
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_origin",
                    "message": "Источник запроса не разрешён",
                    "request_id": request_id,
                }
            },
            status_code=403,
        )
    if int(request.headers.get("content-length", "0") or 0) > 128000:
        return JSONResponse(
            {
                "error": {
                    "code": "body_too_large",
                    "message": "Слишком большой запрос",
                    "request_id": request_id,
                }
            },
            status_code=413,
        )
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 128000:
                return JSONResponse(
                    {
                        "error": {
                            "code": "body_too_large",
                            "message": "Слишком большой запрос",
                            "request_id": request_id,
                        }
                    },
                    status_code=413,
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers.update(
        {
            "X-Request-ID": request_id,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "microphone=(self), camera=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        }
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AppError)
async def app_error(request, error):
    headers = {"Retry-After": "60"} if error.status in (429, 503) else {}
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": getattr(request.state, "request_id", ""),
            }
        },
        status_code=error.status,
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return JSONResponse(
        {
            "error": {
                "code": "validation_error",
                "message": "Проверьте заполненные поля",
                "fields": [
                    {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]} for e in error.errors()
                ],
                "request_id": getattr(request.state, "request_id", ""),
            }
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def unexpected(request, error):
    log.error(
        "request_failed request_id=%s error=%s",
        getattr(request.state, "request_id", ""),
        type(error).__name__,
    )
    return JSONResponse(
        {
            "error": {
                "code": "internal_error",
                "message": "Не удалось выполнить запрос. Повторите позже",
                "request_id": getattr(request.state, "request_id", ""),
            }
        },
        status_code=500,
    )


async def customer(session_id: UUID, authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ")
    if not token or token == authorization:
        raise AppError("unauthorized", "Начните разговор", 401)
    return await app.state.business.session(session_id, token)


async def operator(vdesk_admin: str = Cookie(default="")):
    role = await app.state.pool.fetchval(
        "SELECT role FROM admin_sessions WHERE token_hash=$1 AND expires_at>now()", token_hash(vdesk_admin)
    )
    if not role:
        raise AppError("unauthorized", "Войдите в кабинет", 401)
    return role


async def administrator(role=Depends(operator)):
    if role != "admin":
        raise AppError("forbidden", "Нужны права администратора", 403)
    return role


def channel_authorized(authorization: str = Header(default="")):
    if len(settings.channel_api_key) < 16 or not secrets.compare_digest(
        authorization.encode(), ("Bearer " + settings.channel_api_key).encode()
    ):
        raise AppError("unauthorized", "Требуется ключ канала", 401)


@app.get("/api/v1/call-modes")
async def call_modes():
    return await CallModes(app.state.pool).status()


@app.get("/api/v1/operator/call-settings")
async def call_settings(role=Depends(operator)):
    return await CallModes(app.state.pool).status()


@app.put("/api/v1/operator/call-settings")
async def set_call_settings(data: CallModeInput, role=Depends(administrator)):
    result = await CallModes(app.state.pool).update(data.mode, data.revision, data.incoming_voice)
    if getattr(app.state, "socket_calls", None):
        await app.state.socket_calls.purge()
    return result


@app.post("/api/v1/transport-calls", status_code=201, dependencies=[Depends(channel_authorized)])
async def transport_call(source: str = Form(), profile: str = Form(default="")):
    if source == "telegram" and not profile:
        profile = (await CallModes(app.state.pool).selection()).get("incoming_voice", "male")
    if source not in ("sip", "telegram") or profile not in ("male", "female"):
        raise AppError("invalid_channel", "Некорректный канал или голос", 422)
    if not getattr(app.state, "socket_calls", None):
        raise AppError("channel_unavailable", "Звуковой канал недоступен", 503)
    limit("external-call:" + source, 15)
    return await app.state.socket_calls.register(source, profile)


async def telegram_control(path, data=None, method="POST"):
    import httpx

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.request(
                method,
                settings.telegram_worker_url + path,
                json=data,
                headers={"Authorization": "Bearer " + settings.channel_api_key},
            )
        if response.is_success:
            return response.json()
        error = response.json().get("error", {})
        raise AppError(
            "telegram_authorization",
            error.get("message", "Не удалось подключить Telegram"),
            response.status_code,
        )
    except (httpx.HTTPError, ValueError):
        raise AppError("channel_unavailable", "Сервис Telegram недоступен", 503) from None


@app.post("/api/v1/operator/telegram/authorizations")
async def telegram_authorization(data: TelegramAuthorization, request: Request, role=Depends(administrator)):
    limit("telegram-auth:" + request.client.host, 5, 300)
    return await telegram_control("/authorization", data.model_dump())


@app.post("/api/v1/operator/telegram/authorizations/code")
async def telegram_code(data: TelegramCode, request: Request, role=Depends(administrator)):
    limit("telegram-code:" + request.client.host, 10, 300)
    return await telegram_control("/authorization/code", data.model_dump())


@app.delete("/api/v1/operator/telegram/authorizations")
async def telegram_logout(role=Depends(administrator)):
    return await telegram_control("/authorization", method="DELETE")


@app.get("/api/v1/operator/sip/profile")
async def sip_profile(role=Depends(administrator)):
    import json
    from pathlib import Path

    path = Path(settings.private_runtime_dir) / "sip/metadata.json"
    if not path.is_file():
        raise AppError("sip_not_configured", "SIP ещё не настроен", 404)
    return json.loads(path.read_text())


@app.get("/")
async def home():
    return FileResponse(ROOT / "app/static/index.html")


@app.get("/operator")
async def operator_page():
    return FileResponse(ROOT / "app/static/operator.html")


@app.get("/manage")
async def manage_page():
    return FileResponse(ROOT / "app/static/manage.html")


@app.get("/docs", include_in_schema=False)
async def api_docs():
    return FileResponse(ROOT / "app/static/docs.html")


@app.get("/api/v1/health/live")
async def live():
    return {"status": "alive"}


@app.get("/api/v1/health/ready")
async def ready():
    llm = await app.state.models.llm_ready()
    database = await app.state.pool.fetchval("SELECT 1") == 1
    voice = app.state.models.ready and llm
    selected = (await CallModes(app.state.pool).selection())["mode"]
    tts = (
        await app.state.models.tts_status()
        if hasattr(app.state.models, "tts_status")
        else {"backend": "piper", "natural_ready": False}
    )
    return {
        "status": "ready" if voice else "degraded",
        "database": database,
        "stt": app.state.models.ready,
        "tts": app.state.models.ready,
        "tts_backend": tts["backend"],
        "natural_voice_ready": tts.get("natural_ready", False),
        "stt_backend": settings.stt_backend,
        "llm": llm,
        "voice_available": voice,
        "voice_busy": app.state.voice.active is not None,
        "call_mode": selected,
        "website_calls_enabled": selected == "website",
        "crm_configured": bool(settings.espo_api_key),
        "error": app.state.models.error,
        "profile": "mac-local",
        "language": "ru",
        "timezone": "Europe/Minsk",
    }


@app.get("/api/v1/services")
async def services():
    return {"items": await app.state.business.services(), "test_company": True}


@app.get("/api/v1/voice-profiles")
async def voice_profiles():
    tts = await app.state.models.tts_status()
    return {
        "items": [
            {
                "id": "male",
                "label": "Геннадий",
                "description": "Мягкий низкий мужской голос",
                "available": app.state.models.ready,
            },
            {
                "id": "female",
                "label": "Мария",
                "description": "Тёплый женский голос",
                "available": bool(tts.get("natural_ready")),
            },
        ]
    }


@app.get("/phone", include_in_schema=False)
async def phone_page():
    return FileResponse(ROOT / "app/static/phone.html")


@app.get("/api/v1/mobile")
async def mobile_configuration():
    import json

    path = ROOT / ".runtime/mobile.json"
    return (
        {"configured": True, **json.loads(path.read_text())}
        if settings.lan_enabled and path.exists()
        else {"configured": False}
    )


@app.get("/api/v1/mobile/qr")
async def mobile_qr():
    path = ROOT / ".runtime/mobile-public/setup-qr.svg"
    if not settings.lan_enabled or not path.exists():
        raise AppError("lan_unconfigured", "Доступ с телефона ещё не настроен", 404)
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/api/v1/slots")
async def slots(service_id: str = Query(max_length=60), day: date = Query()):
    return {"items": await app.state.business.slots(service_id, day), "timezone": "Europe/Minsk"}


@app.post("/api/v1/sessions", status_code=201)
async def new_session(data: SessionCreate, request: Request):
    limit("session:" + request.client.host, 10)
    if data.channel == "voice":
        await CallModes(app.state.pool).require("website")
        if not app.state.models.ready or not await app.state.models.llm_ready():
            raise AppError(
                "voice_unavailable", "Голосовые модели ещё не готовы. Доступны чат и форма записи", 503
            )
        if app.state.voice.active is not None:
            raise AppError(
                "voice_busy", "Помощник сейчас разговаривает. Оставьте заявку или попробуйте позже", 409
            )
        if data.voice_profile == "female" and not (await app.state.models.tts_status()).get("natural_ready"):
            raise AppError(
                "voice_unavailable",
                "Женский голос ещё загружается. Подождите немного или выберите мужской голос",
                503,
            )
    return await app.state.business.new_session(data.channel, data.consent, data.voice_profile)


@app.get("/api/v1/sessions/{session_id}")
async def session_detail(session_id: UUID, session=Depends(customer)):
    turns = await app.state.pool.fetch(
        "SELECT id,role,text,created_at FROM turns WHERE session_id=$1 ORDER BY id DESC LIMIT 100", session_id
    )
    actions = await app.state.pool.fetch(
        "SELECT id,result FROM actions WHERE session_id=$1 AND result<>'{}'::jsonb ORDER BY created_at DESC LIMIT 20",
        session_id,
    )
    return {
        "id": str(session_id),
        "state": session["state"],
        "turns": [json_record(r) for r in reversed(turns)],
        "draft": await app.state.business.active_draft(session_id),
        "actions": [app.state.business.action_result(r) for r in actions],
    }


@app.post("/api/v1/sessions/{session_id}/messages")
async def message(session_id: UUID, data: Message, session=Depends(customer)):
    limit("message:" + str(session_id), 20)
    return await app.state.dialogue.message(session_id, data.text, data.idempotency_key)


@app.post("/api/v1/sessions/{session_id}/drafts/bookings", status_code=201)
async def booking_draft(session_id: UUID, data: BookingDraft, session=Depends(customer)):
    return await app.state.business.draft(session_id, "booking", data)


@app.post("/api/v1/sessions/{session_id}/drafts/callbacks", status_code=201)
async def callback_draft(session_id: UUID, data: CallbackDraft, session=Depends(customer)):
    return await app.state.business.draft(session_id, "callback", data)


@app.post("/api/v1/sessions/{session_id}/drafts/{draft_id}/confirmations")
async def confirmation(session_id: UUID, draft_id: UUID, data: Confirmation, session=Depends(customer)):
    result = await app.state.business.confirm(session_id, draft_id, data.revision, data.idempotency_key)
    await app.state.events.emit(session_id, {"type": "action", "result": result})
    return result


@app.delete("/api/v1/sessions/{session_id}/drafts/{draft_id}", status_code=204)
async def cancel_draft(session_id: UUID, draft_id: UUID, session=Depends(customer)):
    await app.state.pool.execute(
        "UPDATE drafts SET status='cancelled' WHERE id=$1 AND session_id=$2 AND status='awaiting_confirmation'",
        draft_id,
        session_id,
    )
    await app.state.pool.execute("UPDATE sessions SET context='{}' WHERE id=$1", session_id)


@app.delete("/api/v1/sessions/{session_id}", status_code=204)
async def end_session(session_id: UUID, session=Depends(customer)):
    await app.state.business.end(session_id)
    await app.state.voice.end(session_id)
    app.state.dialogue.release(session_id)
    await app.state.events.emit(session_id, {"type": "ended"})


@app.post("/api/v1/sessions/{session_id}/offer")
async def offer(session_id: UUID, data: Offer, session=Depends(customer)):
    await CallModes(app.state.pool).require("website")
    if session["channel"] != "voice":
        raise AppError("wrong_channel", "Для микрофона начните голосовой разговор", 409)
    return await app.state.voice.offer(session_id, data.sdp, data.type)


@app.post("/api/v1/sessions/{session_id}/handoffs", status_code=201)
async def handoff(session_id: UUID, session=Depends(customer)):
    available = (
        await app.state.pool.fetchval("SELECT value->>'available' FROM settings WHERE key='operator'")
        == "true"
    )
    if not available:
        return {
            "status": "unavailable",
            "message": "Сотрудник сейчас недоступен. Оставьте заявку на обратный звонок.",
        }
    await app.state.pool.execute(
        "UPDATE sessions SET state='handoff_pending' WHERE id=$1 AND state='bot'", session_id
    )
    await app.state.events.emit(
        session_id,
        {"type": "handoff_pending", "message": "Запрос отправлен сотруднику. Ожидаем ответа до 30 секунд."},
    )
    app.state.voice.request_handoff(session_id)
    return {"status": "pending", "message": "Запрос отправлен сотруднику"}


@app.post("/api/v1/bookings/management")
async def get_management(data: dict, request: Request):
    limit("manage:" + request.client.host, 30)
    token = data.get("token", "")
    if not isinstance(token, str) or len(token) > 200:
        raise AppError("invalid_token", "Недействительная ссылка", 422)
    return await app.state.business.management(token)


@app.post("/api/v1/bookings/management/changes")
async def change_management(data: ManagementChange, request: Request):
    limit("manage:" + request.client.host, 30)
    return await app.state.business.change_booking(data)


@app.post("/api/v1/operator/login")
async def login(data: Login, request: Request, response: Response):
    limit("login:" + request.client.host, 5, 60)
    expected = settings.admin_password if data.role == "admin" else settings.operator_password
    if not expected or not secrets.compare_digest(data.password.encode(), expected.encode()):
        raise AppError("invalid_credentials", "Неверный пароль", 401)
    token = secrets.token_urlsafe(32)
    await app.state.pool.execute(
        "INSERT INTO admin_sessions(token_hash,expires_at,role) VALUES($1,$2,$3)",
        token_hash(token),
        datetime.now(UTC) + timedelta(hours=8),
        data.role,
    )
    response.set_cookie(
        "vdesk_admin",
        token,
        httponly=True,
        secure=settings.cookie_secure or request.headers.get("x-forwarded-proto") == "https",
        samesite="strict",
        max_age=28800,
    )
    await db.audit(app.state.pool, data.role, "login")
    return {"role": data.role}


@app.post("/api/v1/operator/logout", status_code=204)
async def logout(response: Response, vdesk_admin: str = Cookie(default="")):
    await app.state.pool.execute("DELETE FROM admin_sessions WHERE token_hash=$1", token_hash(vdesk_admin))
    response.delete_cookie("vdesk_admin")


@app.get("/api/v1/operator/me")
async def me(role=Depends(operator)):
    return {"role": role}


@app.get("/api/v1/operator/overview")
async def overview(role=Depends(operator)):
    pool = app.state.pool
    counts = await pool.fetchrow(
        "SELECT count(*) AS total,count(*) FILTER(WHERE status='new') AS new,count(*) FILTER(WHERE kind='booking') AS bookings,count(*) FILTER(WHERE kind='callback' AND status<>'done') AS callbacks FROM requests"
    )
    pending = await pool.fetchval("SELECT count(*) FROM outbox WHERE status='pending'")
    active = await pool.fetchval("SELECT count(*) FROM sessions WHERE ended_at IS NULL AND expires_at>now()")
    available = await pool.fetchval("SELECT value FROM settings WHERE key='operator'")
    recent = await pool.fetch(
        "SELECT r.*,bool_and(o.status='synced') AS crm_synced FROM requests r LEFT JOIN outbox o ON o.action_id=r.action_id GROUP BY r.id ORDER BY r.created_at DESC LIMIT 6"
    )
    return {
        **dict(counts),
        "pending_sync": pending,
        "active_sessions": active,
        "operator_available": available["available"],
        "recent": [json_record(r) for r in recent],
    }


@app.get("/api/v1/operator/requests")
async def requests_list(
    limit_: int = Query(default=30, ge=1, le=100, alias="limit"),
    before: datetime | None = None,
    status: str | None = Query(default=None, max_length=30),
    search: str = Query(default="", max_length=100),
    role=Depends(operator),
):
    rows = await app.state.pool.fetch(
        "SELECT r.*,bool_and(o.status='synced') AS crm_synced FROM requests r LEFT JOIN outbox o ON o.action_id=r.action_id WHERE ($1::timestamptz IS NULL OR r.created_at<$1) AND ($2::text IS NULL OR r.status=$2) AND (r.name ILIKE $3 OR r.phone ILIKE $3 OR r.summary ILIKE $3) GROUP BY r.id ORDER BY r.created_at DESC LIMIT $4",
        before,
        status,
        "%" + search + "%",
        limit_ + 1,
    )
    await db.audit(app.state.pool, role, "requests_viewed")
    return {
        "items": [json_record(r) for r in rows[:limit_]],
        "next_cursor": rows[limit_ - 1]["created_at"].isoformat() if len(rows) > limit_ else None,
    }


@app.patch("/api/v1/operator/requests/{rid}")
async def update_request(rid: UUID, data: StatusInput, role=Depends(operator)):
    row = await app.state.pool.fetchrow(
        "UPDATE requests SET status=$2,updated_at=now() WHERE id=$1 RETURNING *", rid, data.status
    )
    if not row:
        raise AppError("not_found", "Обращение не найдено", 404)
    await db.audit(app.state.pool, role, "request_status_changed", rid, {"status": data.status})
    return json_record(row)


@app.delete("/api/v1/operator/requests/{rid}", status_code=204)
async def anonymize(rid: UUID, role=Depends(administrator)):
    from app.privacy import erase_request

    sid = await erase_request(app.state.pool, rid, role)
    if sid:
        await app.state.voice.end(sid)
        app.state.dialogue.release(sid)
        await app.state.events.emit(sid, {"type": "ended"})


@app.get("/api/v1/operator/bookings")
async def bookings(day: date = Query(), role=Depends(operator)):
    from app.business import MINSK

    start = datetime.combine(day, datetime.min.time(), MINSK)
    rows = await app.state.pool.fetch(
        "SELECT id,service_name,price,duration,starts_at,ends_at,name,phone,status,revision FROM bookings WHERE starts_at>=$1 AND starts_at<$2 ORDER BY starts_at LIMIT 100",
        start,
        start + timedelta(days=1),
    )
    await db.audit(app.state.pool, role, "bookings_viewed")
    return {"items": [json_record(r) for r in rows]}


@app.get("/api/v1/operator/sessions")
async def sessions(role=Depends(operator)):
    rows = await app.state.pool.fetch(
        "SELECT id,channel,source,voice_profile,state,created_at,expires_at,ended_at FROM sessions ORDER BY created_at DESC LIMIT 100"
    )
    return {"items": [json_record(r) for r in rows]}


@app.get("/api/v1/operator/sessions/{sid}")
async def operator_session(sid: UUID, role=Depends(operator)):
    row = await app.state.pool.fetchrow(
        "SELECT id,channel,source,voice_profile,state,created_at,ended_at FROM sessions WHERE id=$1", sid
    )
    if not row:
        raise AppError("not_found", "Разговор не найден", 404)
    turns = await app.state.pool.fetch(
        "SELECT role,text,created_at FROM turns WHERE session_id=$1 ORDER BY id LIMIT 200", sid
    )
    await db.audit(app.state.pool, role, "transcript_viewed", sid)
    return {**json_record(row), "turns": [json_record(r) for r in turns]}


@app.put("/api/v1/operator/availability")
async def availability(data: Availability, role=Depends(operator)):
    await app.state.pool.execute("UPDATE settings SET value=$1 WHERE key='operator'", data.model_dump())
    return data


@app.post("/api/v1/operator/sessions/{sid}/handoffs")
async def accept_handoff(sid: UUID, role=Depends(operator)):
    channel = await app.state.pool.fetchval("SELECT channel FROM sessions WHERE id=$1", sid)
    if channel == "voice":
        raise AppError("microphone_required", "Для приёма голосового разговора подключите микрофон", 409)
    row = await app.state.pool.fetchrow(
        "UPDATE sessions SET state='human' WHERE id=$1 AND state='handoff_pending' AND ended_at IS NULL RETURNING channel",
        sid,
    )
    if not row:
        raise AppError("handoff_unavailable", "Этот разговор уже завершён или не ожидает сотрудника", 409)
    await app.state.voice.interrupt(sid)
    await app.state.events.emit(
        sid, {"type": "human", "message": "Сотрудник подключился. Теперь вам отвечает человек."}
    )
    await db.audit(app.state.pool, role, "handoff_accepted", sid)
    return {"status": "human", "channel": row["channel"]}


@app.post("/api/v1/operator/sessions/{sid}/offer")
async def operator_offer(sid: UUID, data: Offer, role=Depends(operator)):
    return await app.state.voice.operator_offer(sid, data.sdp, data.type)


@app.post("/api/v1/operator/sessions/{sid}/messages")
async def operator_message(sid: UUID, data: OperatorReply, role=Depends(operator)):
    state = await app.state.pool.fetchval("SELECT state FROM sessions WHERE id=$1 AND ended_at IS NULL", sid)
    if state != "human":
        raise AppError("handoff_required", "Сначала примите разговор", 409)
    await app.state.pool.execute(
        "INSERT INTO turns(session_id,role,text) VALUES($1,'operator',$2)", sid, data.text
    )
    await app.state.events.emit(sid, {"type": "operator_reply", "text": data.text})
    return {"status": "sent"}


@app.get("/api/v1/operator/knowledge")
async def knowledge_list(role=Depends(operator)):
    rows = await app.state.pool.fetch("SELECT * FROM knowledge ORDER BY updated_at DESC LIMIT 100")
    return {"items": [json_record(r) for r in rows]}


@app.post("/api/v1/operator/knowledge", status_code=201)
async def knowledge_new(data: KnowledgeInput, role=Depends(administrator)):
    row = await app.state.pool.fetchrow(
        "INSERT INTO knowledge(id,title,content,source) VALUES($1,$2,$3,$4) RETURNING *",
        uuid4(),
        data.title,
        data.content,
        data.source,
    )
    await db.audit(app.state.pool, role, "knowledge_created", row["id"])
    return json_record(row)


@app.put("/api/v1/operator/knowledge/{kid}")
async def knowledge_edit(
    kid: UUID, data: KnowledgeInput, revision: int = Query(ge=1), role=Depends(administrator)
):
    row = await app.state.pool.fetchrow(
        "UPDATE knowledge SET title=$2,content=$3,source=$4,revision=revision+1,updated_at=now() WHERE id=$1 AND revision=$5 RETURNING *",
        kid,
        data.title,
        data.content,
        data.source,
        revision,
    )
    if not row:
        raise AppError("revision_conflict", "Документ уже изменён. Обновите список", 409)
    await db.audit(app.state.pool, role, "knowledge_edited", kid)
    return json_record(row)


@app.post("/api/v1/operator/knowledge/{kid}/publications")
async def publish(kid: UUID, data: KnowledgePublish, role=Depends(administrator)):
    row = await app.state.pool.fetchrow(
        "UPDATE knowledge SET published_revision=revision,published_content=content,published_title=title,archived=false,updated_at=now() WHERE id=$1 AND revision=$2 RETURNING *",
        kid,
        data.revision,
    )
    if not row:
        raise AppError("revision_conflict", "Документ изменён. Обновите список", 409)
    await db.audit(app.state.pool, role, "knowledge_published", kid, {"revision": data.revision})
    return json_record(row)


@app.delete("/api/v1/operator/knowledge/{kid}", status_code=204)
async def archive(kid: UUID, role=Depends(administrator)):
    await app.state.pool.execute("UPDATE knowledge SET archived=true,updated_at=now() WHERE id=$1", kid)
    await db.audit(app.state.pool, role, "knowledge_archived", kid)


@app.get("/api/v1/operator/services")
async def all_services(role=Depends(operator)):
    return {
        "items": [dict(r) for r in await app.state.pool.fetch("SELECT * FROM services ORDER BY price,id")]
    }


@app.put("/api/v1/operator/services/{service_id}")
async def update_service(service_id: str, data: ServiceInput, role=Depends(administrator)):
    if data.id != service_id:
        raise AppError("id_mismatch", "Идентификаторы не совпадают", 422)
    row = await app.state.pool.fetchrow(
        "INSERT INTO services(id,name,description,price,duration,active) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO UPDATE SET name=$2,description=$3,price=$4,duration=$5,active=$6,revision=services.revision+1 RETURNING *",
        data.id,
        data.name,
        data.description,
        data.price,
        data.duration,
        data.active,
    )
    await db.audit(app.state.pool, role, "service_updated", service_id)
    return dict(row)


@app.get("/api/v1/operator/schedule")
async def get_schedule(role=Depends(operator)):
    return await app.state.business.schedule()


@app.put("/api/v1/operator/schedule")
async def set_schedule(data: ScheduleInput, role=Depends(administrator)):
    if data.closes <= data.opens:
        raise AppError("invalid_schedule", "Закрытие должно быть позже открытия", 422)
    await app.state.pool.execute(
        "UPDATE settings SET value=$1 WHERE key='schedule'", data.model_dump(mode="json")
    )
    await db.audit(app.state.pool, role, "schedule_updated")
    return data


@app.get("/api/v1/operator/outbox")
async def outbox(role=Depends(operator)):
    rows = await app.state.pool.fetch(
        "SELECT id,action_id,entity,status,attempts,next_attempt_at,external_id,error,created_at FROM outbox ORDER BY created_at DESC LIMIT 100"
    )
    return {"items": [json_record(r) for r in rows]}


@app.post("/api/v1/operator/outbox/{oid}/retries")
async def retry(oid: UUID, role=Depends(operator)):
    await app.state.pool.execute(
        "UPDATE outbox SET next_attempt_at=now() WHERE id=$1 AND status='pending'", oid
    )
    return {"status": "queued"}


@app.get("/api/v1/operator/metrics")
async def metrics(role=Depends(operator)):
    data = await ready()
    data["metrics"] = app.state.models.stats
    data["voice"] = app.state.voice.metrics
    data["last_crm_sync"] = app.state.crm.last_ok.isoformat() if app.state.crm.last_ok else None
    return data


@app.get("/api/v1/operator/audit")
async def audit_list(before: int | None = None, role=Depends(administrator)):
    rows = await app.state.pool.fetch(
        "SELECT * FROM audit WHERE ($1::bigint IS NULL OR id<$1) ORDER BY id DESC LIMIT 50", before
    )
    return {
        "items": [json_record(r) for r in rows],
        "next_cursor": rows[-1]["id"] if len(rows) == 50 else None,
    }


@app.websocket("/api/v1/sessions/{session_id}/events")
async def events(websocket: WebSocket, session_id: UUID):
    if websocket.headers.get("origin") not in settings.allowed_origins.split(","):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue = None
    sender = None
    try:
        limit("websocket:" + websocket.client.host, 60)
        auth = await asyncio.wait_for(websocket.receive_json(), 5)
        if auth.get("operator"):
            role = await app.state.pool.fetchval(
                "SELECT role FROM admin_sessions WHERE token_hash=$1 AND expires_at>now()",
                token_hash(websocket.cookies.get("vdesk_admin", "")),
            )
            if not role:
                raise AppError("unauthorized", "Нет доступа", 401)
        else:
            await app.state.business.session(session_id, auth.get("token", ""))
        queue = app.state.events.subscribe(session_id)

        async def send():
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 20)
                except TimeoutError:
                    event = {"type": "ping"}
                if auth.get("operator"):
                    valid = await app.state.pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM admin_sessions WHERE token_hash=$1 AND expires_at>now())",
                        token_hash(websocket.cookies.get("vdesk_admin", "")),
                    )
                else:
                    valid = await app.state.pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM sessions WHERE id=$1 AND ended_at IS NULL AND expires_at>now())",
                        session_id,
                    )
                if not valid:
                    await websocket.close(code=1008)
                    return
                await websocket.send_json(event)

        sender = asyncio.create_task(send())
        while True:
            message = await websocket.receive_json()
            limit("ws-message:" + str(session_id), 30)
            if message.get("type") == "interrupt" and not auth.get("operator"):
                await app.state.voice.interrupt(session_id)
    except (WebSocketDisconnect, AppError, TimeoutError, ValueError, KeyError, TypeError):
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
    finally:
        if sender:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if queue:
            app.state.events.unsubscribe(session_id, queue)


app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
