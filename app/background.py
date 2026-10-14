"""Retry background I/O with bounded backoff and durable, non-sensitive diagnostics."""

import asyncio
import contextlib
from datetime import UTC, datetime

from app.telemetry import event, failure


async def run_background(name, action, *, interval, stop=None, status=None, initial_delay=False):
    stop = stop or asyncio.Event()
    status = status if status is not None else {}
    status.update(healthy=True, failures=0, error=None)
    delay = interval if initial_delay else 0
    while not stop.is_set():
        if delay:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break
            except TimeoutError:
                pass
        try:
            worked = await action()
        except Exception as exc:
            status.update(healthy=False, failures=status["failures"] + 1, error=type(exc).__name__)
            delay = min(60, interval * 2 ** min(status["failures"], 6))
            if status["failures"] == 1:
                event("background_failed", component=name, error_type=status["error"], retry_seconds=delay)
                with contextlib.suppress(OSError):
                    failure(name, status["error"])
        else:
            if not status["healthy"]:
                event("background_recovered", component=name, failures=status["failures"])
            status.update(healthy=True, failures=0, error=None, last_ok=datetime.now(UTC).isoformat())
            delay = 0 if worked else interval

