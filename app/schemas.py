import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SessionCreate(BaseModel):
    channel: Literal["chat", "voice"] = "chat"
    consent: bool
    voice_profile: Literal["male", "female"] = "male"


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: UUID


class BookingDraft(BaseModel):
    service_id: str = Field(max_length=60)
    starts_at: datetime
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(max_length=30)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if len(value) < 2 or any(ord(c) < 32 for c in value):
            raise ValueError("Укажите имя")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value):
        value = re.sub(r"[\s()\-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("Укажите телефон с кодом страны, например +375291234567")
        return value

    @field_validator("starts_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Нужен часовой пояс")
        return value


class CallbackDraft(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(max_length=30)
    message: str = Field(min_length=3, max_length=2000)
    _phone = field_validator("phone")(BookingDraft.normalize_phone.__func__)


class Confirmation(BaseModel):
    revision: int = Field(ge=1)
    idempotency_key: UUID


class Login(BaseModel):
    password: str = Field(max_length=200)
    role: Literal["admin", "operator"] = "admin"
    username: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{2,60}$")


class ServiceInput(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(max_length=2000)
    price: int = Field(ge=0, le=100000)
    duration: int = Field(ge=15, le=240, multiple_of=15)
    active: bool = True


class KnowledgeInput(BaseModel):
    title: str = Field(min_length=2, max_length=150)
    content: str = Field(min_length=5, max_length=20000)
    source: str = Field(max_length=300, default="Администратор")


class KnowledgePublish(BaseModel):
    revision: int = Field(ge=1)


class OperatorReply(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class StatusInput(BaseModel):
    status: Literal["new", "in_progress", "done"]


class Availability(BaseModel):
    available: bool


class CallModeInput(BaseModel):
    mode: Literal["website", "telegram", "sip"]
    revision: int = Field(ge=1)
    incoming_voice: Literal["male", "female"] | None = None


class TelegramAuthorization(BaseModel):
    api_id: int = Field(ge=1)
    api_hash: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


class TelegramCode(BaseModel):
    code: str = Field(default="", pattern=r"^\d{0,8}$")
    password: str = Field(default="", max_length=300)


class ScheduleInput(BaseModel):
    opens: int = Field(ge=0, le=22)
    closes: int = Field(ge=1, le=23)
    weekdays: list[int] = Field(min_length=1, max_length=7)
    closed_dates: list[date] = Field(default_factory=list, max_length=365)

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, days):
        if len(set(days)) != len(days) or any(d < 0 or d > 6 for d in days):
            raise ValueError("Дни недели: 0–6, без повторов")
        return days


class ManagementChange(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    operation: Literal["cancel", "reschedule"]
    starts_at: datetime | None = None
    revision: int = Field(ge=1)
    confirm: bool = False


class Offer(BaseModel):
    sdp: str = Field(min_length=10, max_length=100000)
    type: Literal["offer"]


class PlaybackReceipt(BaseModel):
    played_chars: int = Field(ge=0, le=5000)


class ExtractedIntent(BaseModel):
    intent: Literal["faq", "conversation", "booking", "callback", "cancel_draft", "handoff", "unknown"] = "unknown"
    service_id: str | None = None
    name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    date: str | None = Field(default=None, max_length=10)
    time: str | None = Field(default=None, max_length=5)
    message: str | None = Field(default=None, max_length=2000)
    answer: str = Field(default="", max_length=1500)
    fact_quotes: list[str] = Field(default_factory=list, max_length=3)
    category: str | None = Field(default=None, max_length=80)
