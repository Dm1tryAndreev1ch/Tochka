"""Enable consent-gated recording for new calls and audit the owner's decision."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.policy import enable_audio_recording  # noqa: E402


async def main():
    pool = await db.connect(settings.database_url)
    try:
        company = await enable_audio_recording(pool, actor="owner_request")
        policy = company["settings"]
        print(f"Recording enabled for new calls after oral permission; policy version {policy['policy_version']}; audio retention {policy['audio_days']} days.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
