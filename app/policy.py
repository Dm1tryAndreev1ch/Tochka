"""Versioned disclosure generated from actual retention and recording settings."""

from app import db


async def enable_audio_recording(pool, *, actor):
    """Apply the owner's recording preference without changing existing notices."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM company WHERE id=1 FOR UPDATE")
        if row["settings"].get("record_audio"):
            return dict(row)
        policy = {**row["settings"], "record_audio": True}
        policy["policy_version"] = policy.get("policy_version", 1) + 1
        updated = await conn.fetchrow(
            "UPDATE company SET settings=$1,revision=revision+1 WHERE id=1 RETURNING *", policy,
        )
        await db.audit(conn, actor, "audio_recording_enabled", "1", {
            "revision": updated["revision"], "policy_version": policy["policy_version"],
            "requires_oral_permission": True,
        })
        return dict(updated)


async def current_policy(pool):
    row = await pool.fetchrow("SELECT name,settings FROM company WHERE id=1")
    return {"company_name": row["name"], **row["settings"]}


def notice(policy):
    text = f"Я виртуальный помощник компании {policy.get('company_name', 'Точка')}. Текст разговора и данные заявки сохраняются на {policy.get('text_days', 30)} дней. "
    if policy.get("record_audio"):
        return text + f"С вашего разрешения разговор будет записан; аудиозапись хранится {policy.get('audio_days', 30)} дней. Для разрешения скажите «разрешаю запись разговора». Если вы не разрешаете, скажите «не разрешаю»."
    return text + "Аудиозапись не сохраняется. Для продолжения скажите «согласен» или «разрешаю», для отказа — «не согласен»."
