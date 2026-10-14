"""Individual staff identities and explicit permissions for a single-company deployment."""

import hashlib
import hmac
import secrets
from uuid import uuid4

from app.config import settings
from app.errors import AppError

ALL_PERMISSIONS = {
    "requests.read",
    "calls.live",
    "knowledge.read",
    "knowledge.write",
    "knowledge.publish",
    "agents.read",
    "agents.write",
    "channels.read",
    "channels.manage",
    "channels.secrets",
    "company.read",
    "company.manage",
    "staff.manage",
    "archive.metadata",
    "archive.text",
    "archive.audio",
    "archive.export",
    "privacy.delete",
    "audit.read",
    "metrics.read",
    "crm.read",
    "crm.retry",
}
ROLE_PERMISSIONS = {
    "admin": ALL_PERMISSIONS,
    "operator": {
        "requests.read",
        "calls.live",
        "knowledge.read",
        "agents.read",
        "channels.read",
        "company.read",
        "metrics.read",
        "crm.read",
    },
    "auditor": {"archive.metadata", "audit.read", "metrics.read", "company.read"},
    "technical": {"metrics.read", "channels.read", "company.read"},
}


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    return "scrypt1:" + salt.hex() + ":" + digest.hex()


def password_matches(password, encoded):
    try:
        _, salt, expected = encoded.split(":")
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024
        )
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class Principal(str):
    def __new__(cls, row, session_hash=None):
        obj = super().__new__(cls, row["role"])
        obj.user_id = row["id"]
        obj.username = row["username"]
        obj.display_name = row["display_name"]
        obj.permissions = frozenset(row["permissions"])
        obj.session_hash = session_hash
        return obj

    def require(self, permission):
        if permission not in self.permissions:
            raise AppError("forbidden", "У вашей учётной записи нет доступа к этому действию", 403)


async def bootstrap_staff(pool):
    if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM staff_users)"):
        return
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7860004)")
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM staff_users)"):
            return
        for role, password in (("admin", settings.admin_password), ("operator", settings.operator_password)):
            if len(password) < 16:
                raise RuntimeError("Initial staff passwords must have at least 16 characters")
            await conn.execute(
                "INSERT INTO staff_users(id,username,display_name,password_hash,role,permissions) VALUES($1,$2,$3,$4,$5,$6)",
                uuid4(),
                role,
                "Владелец" if role == "admin" else "Оператор",
                password_hash(password),
                role,
                sorted(ROLE_PERMISSIONS[role]),
            )


def route_permission(path, method):
    suffix = path.removeprefix("/api/v1/operator/")
    if suffix in ("me", "logout"):
        return None
    if suffix.startswith("archive"):
        return "archive.metadata"
    if suffix.startswith("staff"):
        return "staff.manage"
    if suffix.startswith("company"):
        return "company.read" if method == "GET" else "company.manage"
    if suffix.startswith("agents"):
        return "agents.read" if method == "GET" else "agents.write"
    if suffix.startswith("audit"):
        return "audit.read"
    if suffix.startswith(("metrics", "usage")):
        return "metrics.read"
    if suffix.startswith(("outbox", "crm")):
        return "crm.read" if method == "GET" else "crm.retry"
    if suffix.startswith("knowledge"):
        return (
            "knowledge.read"
            if method == "GET"
            else "knowledge.publish"
            if suffix.endswith("publications")
            else "knowledge.write"
        )
    if suffix.startswith(("services", "schedule")):
        return "agents.read" if method == "GET" else "agents.write"
    if suffix.startswith(("call-settings", "telegram", "sip")):
        return (
            "channels.secrets"
            if suffix.startswith("sip")
            else "channels.read"
            if method == "GET"
            else "channels.manage"
        )
    if suffix.startswith(("sessions", "availability")):
        return "calls.live"
    if suffix.startswith(("overview", "requests", "bookings")):
        return "privacy.delete" if method == "DELETE" else "requests.read"
    return "staff.manage"
