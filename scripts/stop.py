import os
import signal
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
pidfile = root / ".runtime/launcher.pid"
if not pidfile.exists():
    raise SystemExit("Нет запущенного процесса приложения")
pid = int(pidfile.read_text())
command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
if "scripts/run.py" not in command:
    raise SystemExit("PID-файл устарел; посторонний процесс не остановлен")
os.kill(pid, signal.SIGTERM)
print("Остановка приложения запрошена. PostgreSQL и CRM продолжают работать.")
