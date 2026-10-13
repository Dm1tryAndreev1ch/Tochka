import json
import logging
from datetime import UTC, datetime

from app.config import ROOT

log = logging.getLogger("voice.events")


def event(name, **values):
    # Callers supply timings, backend names and ids, never raw text or credentials.
    record = {"event": name, "at": datetime.now(UTC).isoformat(), **values}
    log.info(json.dumps(record, ensure_ascii=False))


def failure(component, error_type):
    path = ROOT / ".runtime" / f"{component}-error.json"
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"component": component, "error_type": error_type, "at": datetime.now(UTC).isoformat()})
    )
    temporary.chmod(0o600)
    temporary.replace(path)
