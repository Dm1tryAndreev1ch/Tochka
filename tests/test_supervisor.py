import asyncio
import sys

from app.runtime_environment import build_child_environment
from app.supervisor import ProcessSupervisor


async def test_failed_owned_process_is_restarted_and_reported(tmp_path):
    marker = tmp_path / "attempt"
    code = "from pathlib import Path;import sys,time;p=Path(sys.argv[1]);n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n));time.sleep(10) if n>1 else sys.exit(7)"
    supervisor = ProcessSupervisor(tmp_path, retry_base=0.01)
    try:
        supervisor.start("test", [sys.executable, "-c", code, str(marker)], env=build_child_environment("llm"))
        for _ in range(100):
            supervisor.tick()
            if marker.exists() and marker.read_text() == "2":
                break
            await asyncio.sleep(0.02)
        assert marker.read_text() == "2"
        status = supervisor.snapshot()["test"]
        assert status["restarts"] == 1
        assert status["last_exit"] == 7
        assert status["state"] == "running"
    finally:
        supervisor.close()
