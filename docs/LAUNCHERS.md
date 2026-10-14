# Запуск на macOS, Linux и Windows

Общая точка входа — `scripts/launch.py`. На macOS используйте `Launch.command`, на Linux —
`./Launch.sh`, на Windows — `Launch.cmd` или `Launch.ps1`. Обёртки переходят в папку проекта
и предпочитают её `.venv`, поэтому пробелы в пути допустимы. Запуск без аргументов в терминале
показывает меню. Аргументы в примерах одинаковы у всех обёрток. На Windows при отсутствии
`.venv` сначала проверяется установленный Python 3.12, затем 3.13 через `py`; автоматическая
установка интерпретатора отключена. Если подходящего Python нет, обёртка выводит инструкцию.

Есть два режима приложения: `--mode local` запускает LLM на этом же компьютере;
`--mode distributed` подключает приложение к уже запущенному LLM по HTTP API. Режим
`--role llm` запускает только сервер GGUF, без PostgreSQL, CRM, микрофона и TTS.

| Компьютер приложения | Речь | LLM |
| --- | --- | --- |
| Apple Silicon macOS | Настройки `.env`, включая whisper.cpp/Metal и нативный MLX TTS | Локальный Metal или отдельный сервер |
| Linux / Windows | Явный `--speech portable`: faster-whisper CPU и Piper Denis | Локальный llama.cpp CPU/CUDA или отдельный сервер |
| Windows / Linux GPU-сервер | Только `--role llm`; речь выполняется на компьютере приложения | Установленный llama-server со сборкой CUDA и совместимыми драйверами |

Переносимый профиль меняет возможности речи: женский эталонный MLX-голос в нём недоступен,
а мужской Piper отличается от утверждённого клонированного голоса. Приложение отклоняет выбор
недоступного женского голоса; автоматической замены его мужским нет. MLX/Metal не запускается
в Windows, Linux или Linux-контейнере Docker на Mac. Настоящая работа аудио на Windows/Linux
должна проверяться на целевом компьютере: эти обёртки не являются доказательством такой проверки.

## Подготовка

Нужны Python 3.12 или 3.13 и, для приложения, Docker с Compose и запущенным engine. Для
локального LLM/отдельного GPU-сервера нужен установленный `llama-server` из llama.cpp.
CUDA выбирается сборкой сервера и драйверами; флаг GPU layers сам по себе поддержку не добавляет.

На существующем Mac сохраняется подготовка через `scripts/setup.py`. Она зависит от Homebrew
и macOS; её нельзя использовать как установщик для Windows/Linux. Для Metal ASR также нужна
собранная `.runtime/libvoice_whisper.dylib` (см. `scripts/build_whisper.py`).

Для переносимого окружения Linux:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-portable.txt
```

Для Windows в PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-portable.txt
```

`requirements-portable.txt` фиксирует прямые зависимости по текущему lock; это не отдельный
полный lock для каждой ОС. Не устанавливайте Mac `requirements.lock` или `pip install .`
в переносимое окружение: они содержат MLX. Telegram-звонки требуют отдельно совместимых
`py-tgcalls==3.0.0` и `telethon==1.45.0`; при включённом `CHANNEL_API_KEY` doctor проверит их наличие.
Если они не нужны, оставьте этот ключ пустым или добавьте `--no-telegram`. SIP/Telegram и системные аудиоустройства требуют
отдельной проверки на выбранной ОС; переносимость launcher не обещает их универсальности.

Создайте `.env` по `.env.example`: задайте `ADMIN_PASSWORD` (от 16 символов),
`OPERATOR_PASSWORD` (от 16, нужен для создания сотрудника на свежей БД), `MANAGEMENT_SECRET`
(от 32), `LLM_API_KEY` (от 16) и остальные настройки сервиса. Пароль БД в `DATABASE_URL`
должен совпадать с `POSTGRES_PASSWORD` для Docker Compose.
Для Mac TTS задайте `NATURAL_TTS_API_KEY`. Ключи не передаются аргументами командной строки.
Launcher поддерживает обычные однострочные `KEY=value`, кавычки и комментарии в `.env`;
подстановки `${...}` и многострочные значения нужно заменить буквальными значениями
или переменными окружения. Приоритет: `.env` → окружение процесса → явные параметры запуска.

## Модели и диагностика

```sh
./Launch.sh --speech mac-metal --download-only    # Mac: комплект mac-metal
./Launch.sh --speech portable --download-only     # переносимый комплект
./Launch.sh --role llm --download-only            # только GGUF для LLM-сервера
./Launch.sh --doctor                             # только локальные проверки
./Launch.sh --mode distributed --dry-run          # только план, без сети и изменений
```

На Windows замените `./Launch.sh` на `.\Launch.cmd`, на macOS можно на `./Launch.command`.
Загрузка моделей вызывается явно и может занимать гигабайты; простой `--doctor` ничего не скачивает.
`--download-models` скачивает модели и затем запускает выбранную роль, `--download-only` только
скачивает. `--dry-run` имеет приоритет и не скачивает даже вместе с этими флагами.
`--doctor --download-models` также ничего не скачивает.

Загрузчик `scripts/fetch_models.py` использует профили `mac-metal`, `portable`, `llm-worker`;
его собственный `--dry-run` показывает точный состав и объём. Можно задать `--model-dir PATH`:
launcher использует стандартные имена каталога из этой папки, включая voice references на Mac.
При загрузке стандартные пути применяются к запускаемым процессам, существующий `.env` не переписывается.
При следующем запуске повторите `--model-dir PATH` либо перенесите соответствующие пути в `.env`.
Собственный GGUF указывается через `--model-path PATH --model ALIAS`; для него не применяйте
`--download-models`, скачивающий стандартный GGUF каталога.

Профиль `--speech configured` сохраняет речь из `.env` и подбирает комплект по фактическим
STT/TTS-настройкам. Явный `--speech mac-metal` включает whisper.cpp и MLX; `--speech portable`
включает faster-whisper и Piper. Смешанные настройки faster-whisper + MLX требуют обоих
каталогов (общие файлы проверяются и повторно не загружаются). Для distributed-приложения
launcher передаёт `--exclude-llm`: GGUF загружается только на компьютере LLM-сервера.

Doctor проверяет наличие файлов, пакетов, настроек и исполняемых файлов. Он не подключается к
LLM, не запускает Docker и не измеряет задержку/качество речи. Для worker достаточно стандартной
библиотеки Python: пакеты приложения устанавливать не обязательно.

## Локальное приложение

```sh
./Launch.command --mode local --download-models
./Launch.sh --mode local --speech portable --download-models
```

Windows:

```powershell
.\Launch.cmd --mode local --speech portable --download-models
```

Приложение использует существующий supervisor `scripts/run.py`, включая Docker Compose,
миграции/готовность сервиса и cleanup принадлежащих ему дочерних процессов. Для локального
режима `llama-server` должен быть в `PATH`, а `LLM_URL` — `http://127.0.0.1:18082/v1` или другой
loopback HTTP endpoint с `/v1`. Настройки `LLM_GPU_LAYERS`, `LLM_CONTEXT_TOKENS`, `LLM_THREADS`
берутся из `.env`. Для остановки запуска из терминала используйте Ctrl+C. Старый `Stop.command`
и `scripts/stop.py` не предлагаются как Windows-управление процессами.

## Приложение + Windows GPU

Создайте на GPU-компьютере приватный текстовый файл `llm-key.txt` с одним случайным токеном
не менее 32 символов. Тот же токен задайте как `LLM_API_KEY` на компьютере приложения или
передайте его файлом через `--llm-api-key-file`. Не кладите ключ в Git. На Unix ограничьте
права файла до `0600`, на Windows используйте ACL только для нужной учётной записи;
launcher не утверждает, что POSIX chmod обеспечивает Windows ACL.

На Windows GPU-компьютере:

```powershell
.\Launch.cmd --role llm --download-models --server-binary "C:\llama.cpp\llama-server.exe" --llm-api-key-file "C:\VoiceSecrets\llm-key.txt"
```

По умолчанию сервер слушает только `127.0.0.1:18082`. Для прямого соединения по частной сети
нужно явно добавить `--bind <IP-GPU-компьютера>` и разрешить входящие подключения в firewall
только с компьютера приложения/VPN. HTTP с bearer token сам по себе не шифрует трафик.
Используйте защищённую частную сеть/VPN, либо TLS proxy/аутентифицированный туннель;
не публикуйте порт открыто в Интернет. Native mTLS/пользовательский CA этот launcher не настраивает.

На компьютере приложения, например Mac:

```sh
./Launch.command --mode distributed --llm-url http://10.20.0.2:18082/v1 --llm-api-key-file ~/VoiceSecrets/llm-key.txt
```

Или Linux/Windows с `--speech portable`. Можно использовать loopback endpoint уже установленного
защищённого туннеля: `--llm-url http://127.0.0.1:28082/v1`. Режим distributed устанавливает
`LLM_REMOTE=true`: сервер обязан показать настроенный alias в `/v1/models`, но его Windows-путь
GGUF не сравнивается с локальным путём Mac. Недоступный remote останавливает запуск до Docker;
последующий обрыв также не приводит к запуску локального LLM. Это не автоматический failover.

Alias `--model` на worker и `LLM_MODEL` приложения должны совпадать; по умолчанию это `qwen3-4b`.
Смена профиля не меняет уже работающий gateway: сначала остановите прежний запуск и запустите заново.
Windows/Linux не используют автоматическую Mac LAN-настройку. Переносимый профиль задаёт
`LAN_ENABLED=false`; для браузеров на других устройствах отдельно настройте HTTPS, host/origin
allowlist и доступность WebRTC. При работе на том же компьютере подходит localhost.
