"""Own, restart and report only the child processes started by this launcher."""

import json
import logging
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


class ProcessSupervisor:
    def __init__(self, runtime_dir, retry_base=2, cwd=None):
        self.runtime = Path(runtime_dir)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.retry_base, self.cwd = retry_base, cwd
        self.records = {}

    def _launch(self, record):
        process = subprocess.Popen(record["command"], cwd=self.cwd, env=dict(record["env"]), stdout=subprocess.PIPE if record["log"] else subprocess.DEVNULL, stderr=subprocess.STDOUT)
        record.update(process=process, started=time.monotonic(), state="running")
        if record["log"]:
            def drain():
                handler = RotatingFileHandler(record["log"], maxBytes=10 * 1024 * 1024, backupCount=2, encoding="utf-8")
                try:
                    for line in iter(process.stdout.readline, b""):
                        handler.emit(logging.LogRecord(record["name"], logging.INFO, "", 0, line.decode("utf-8", errors="replace").rstrip(), (), None))
                finally:
                    handler.close()
                    process.stdout.close()
            threading.Thread(target=drain, daemon=True).start()
        return process

    def start(self, name, command, log=None, *, env):
        if name in self.records:
            raise ValueError("Child name is already registered")
        environment = dict(env)
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
            raise TypeError("Child environment keys and values must be strings")
        if log:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
        record = {"name": name, "command": command, "env": environment, "log": log, "restarts": 0, "failures": 0, "last_exit": None, "retry_at": 0}
        self.records[name] = record
        try:
            process = self._launch(record)
        except OSError:
            del self.records[name]
            raise
        self._save()
        return process

    def tick(self):
        changed = False
        for record in self.records.values():
            exit_code = record["process"].poll()
            if exit_code is None:
                if time.monotonic() - record["started"] > 60:
                    record["failures"] = 0
                continue
            if record["state"] == "running":
                record.update(state="waiting", last_exit=exit_code, failures=record["failures"] + 1)
                record["retry_at"] = time.monotonic() + min(60, self.retry_base * 2 ** min(record["failures"] - 1, 5))
                changed = True
            if time.monotonic() >= record["retry_at"]:
                try:
                    self._launch(record)
                    record["restarts"] += 1
                except OSError as exc:
                    record.update(error=type(exc).__name__, retry_at=time.monotonic() + 60)
                changed = True
        if changed:
            self._save()

    def snapshot(self):
        return {name: {"pid": r["process"].pid, "state": r["state"], "restarts": r["restarts"], "last_exit": r["last_exit"]} for name, r in self.records.items()}

    def _save(self):
        temporary = self.runtime / "processes.tmp"
        temporary.write_text(json.dumps(self.snapshot(), indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.runtime / "processes.json")

    def close(self):
        for record in reversed(list(self.records.values())):
            process = record["process"]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            record["state"] = "stopped"
        self._save()
