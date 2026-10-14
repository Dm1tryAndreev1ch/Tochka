# Передача разработки — 13 октября 2026

Читать с `docs/STATE.md` и `docs/continue.md`. Разработка и приёмка не закончены. Пользователь просит сообщение для продолжения в новом чате, а не новый чат автоматически.

## Требования и принятые решения

Продолжить план: критические ошибки ядра D01–D05, затем качество D06/D07. **Слова≥94% (WER≤6%) и намерения≥94% независимо и одновременно.** Улица/офис, эхо, быстрый темп, паузы, телефонный звук8кГц, живые голоса разных людей. Пользователь выбрал сначала открытые корпуса. NLU измерять по аудио→STT→NLU, не эталонному тексту.

Дополнительно: безопасность персональных данных и записей; кабинет компании, персональные сотрудники/права, настройки агентов/знаний под аудитом; архив с целью доступа; краткие последние отправления CRM и графики; FIFO ожидание при занятом ИИ; время/LLM-токены каждого звонка и дашборд; предупреждение и **устное** разрешение записи, например «разрешаю».

Геннадий — одобренный тембр, скорость1,10 без изменения высоты. Мария — выбранный «1 — низкий бархатный», `maria-velvet`. Не менять голоса. Телефоны iPhone/Android, сначала одна сеть. Telegram нужен обычный звонок отдельному аккаунту; подключение аккаунта пользователь отложил. SIP-провайдер не выбран, нужен бесплатный/недорогой; номера не заказывать автоматически.

Исходное ТЗ: `docs/requirements/OPERATOR_TZ_SOURCE.txt`, SHA256 `c5bdfc803040dd15370737520e8cfe1b1da8ffcd9d3846ff427d8937d4fea0b4`. План D00–D29: `docs/DEVELOPMENT_BACKLOG.md`; матрица: `docs/TZ_COMPLIANCE.md`; методика: `docs/VALIDATION.md`. Документы ещё нужно обновить по фактическим изменениям, критерии приёмки не закрыты.

## Окружение

Mac M4/16GB, Python3.12, `.venv` в cache. На момент передачи приложение/модели/эксперимент остановлены, localhost7860 недоступен, сокета Docker OrbStack нет. Причина остановки не исследована; данные не сбрасывались. Проверить отсутствие другого экземпляра, затем штатный launcher:

```sh
PYTHONPYCACHEPREFIX=/Users/andreevich/.cache/voice-desk/pycache .venv/bin/python scripts/run.py
PYTHONPYCACHEPREFIX=/Users/andreevich/.cache/voice-desk/pycache .venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONPYCACHEPREFIX=/Users/andreevich/.cache/voice-desk/pycache .venv/bin/python -m ruff check --no-cache app scripts tests
npm run check
npx playwright test tests/browser/design-workspace.spec.js tests/browser/call-queue.spec.js
```

Launcher умеет поднимать OrbStack/Docker, надзирать за своими процессами. БД15432, CRM18081, LLM18082, TTS18083, Telegram18084, gateway7860, HTTPS8443, mobile setup8765. Старые exec session IDs не использовать. `scripts/stop.py` останавливает подтверждённый launcher, не сбрасывая БД.

Последний LAN `172.20.10.3` вместо `192.168.1.119`, при старте launcher обновляет IP. Ранее mobile-setup оставался со старым argv; нужен полный restart. CA сохранён, fingerprint `9C2F947F8AA9B1D58D3B008A7DD6CB0D69E8849892773A8927680F5831D6D9A8`. Private keys в `~/.cache/voice-desk/private/lan-ca` и `lan-server-key.pem`, публичный .cer в `.runtime/mobile-public`. Для Python SSL использовать PEM `private/lan-ca/rootCA.pem`, не DER .cer.

Много намеренных незакоммиченных изменений, включая другую работу по дизайну. Не делать reset/checkout/clean. HEAD на старте `9fc9ffeea37199ae8930a11ab447f05c1c411fb3`, commit/PR не заказаны. `AGENTS.md`/`docs/design/DESIGN_SYSTEM.md`: Golos, tokens/components, SVG не менять; `/landing` и `/operator` отдельны, `/` голосовой клиент; реальные данные и права. Сохранить чужие landing/brand/operator-workspace/styles/дизайн-тесты. Свободно около6,5GiB, не удалять рабочие данные ради места.

## Код и границы реализации

### Ядро и очередь

- `background.py`: CRM/housekeeping retry/backoff и безопасные причины отказа. Readiness при потере БД503. `supervisor.py`/`scripts/run.py`: owned children, bounded retry60с, PID/state, log rotation10MiB×3. Initial model load retry и полнота readiness ещё требуют работы.
- `grounding.py`: телефон из опубликованного источника; factual FAQ через exact quotes, guards чисел/заявлений о завершённых действиях. Это не полное доказательство отсутствия hallucination. Последний fix не перехватывает телефон самого клиента как FAQ; широкие «ваш/у вас» триггеры ослаблены.
- Память имени отдельно на сессию;32 реплики; tokenizer llama.cpp apply-template/tokenize,8192 context,384 reply и128 запас; отсечение старых пар. Overflow422, документы выбираются целиком.
- Миграция005/dialogue/voice: generated_text отдельно от слышимого text; в LLM history подтверждённая сыгранная часть. SpeechChunkSent после paced output, браузерная receipt защищена от поздней квитанции после interruption. SIP/Telegram без receipt, история консервативна.
- LongSTT:20с segments/0,5с overlap, один финальный transcript;90с предел. Native Whisper уже no_context=true, полный encoder context; не исправлять несуществующую previous-call leak.
- Голосовая просьба сотрудника→настоящее handoff, единое действие Business, timeout30с/недоступность/отключение озвучиваются. Operator media привязан к Principal/assigned_user_id, отзыв при logout/staff disable, guard WS.
- `call_queue.py`: FIFO7 ожидающих, timeout300с, позиция/выход/full429; AdmissionGate перед VAD/STT не обрабатывает ожидающего. Два реальных WebRTC peers проверялись. Telegram worker пока один bridge, multi-call queue не доказана. Слот остаётся занят при разговоре с человеком, ёмкость/timeout только env.

### Компания, права, аудит, дашборд

- `access.py`: staff_users/scrypt/permissions, роль из БД, bootstrap admin/operator из существующих паролей. Можно создавать индивидуальные учётки; начальная общая operator ещё существует.
- `workspace.py`: company/staff/agents/usage/crm-summary API, revision conflicts, staff session revoke. Снимок инструкций/знаний/allowed_actions для новых разговоров. Миграция008 category_schema и классификация модели есть, редактирование category_schema через AgentUpdate/UI ещё нет.
- `db.py`: RequestPool/ContextVar общая SQL-транзакция mutation+audit, active flag для spawned tasks; HMAC цепочка actor/user/request/purpose/outcome. Нет независимого checkpoint/verifier и DB append-only прав.
- `operator-workspace.js`/`protected-workspace.js`: company/agents/staff/usage и archive/CRM/audit. Новые protected sections и последняя usage правка ещё требуют wide/mobile QA.
- Usage: реальные LLM prompt/completion/requests, неизвестный расход отдельно. Последняя правка учитывает только подключённые звонки, очередь ушедших до ответа, per-call talk/wait/connection и daily; UI таблица/SVG звонков, токенов, разговора, ожидания. Время включает уведомление, паузы и сотрудника. Токены только LLM; STT/TTS-токены и стоимость не реализованы.

### Устное согласие, архив, backup

- Policy из настроек компании, version snapshot. Voice без checkbox допустим, chat требует согласие. Audio recording по умолчанию выключен, владелец включает.
- `voice.py`: durable audio начинается только после oral permission, включая «разрешаю»; отказ не записывает. NoticeSent должен завершиться, новое начало речи≥0,5с позже. Последняя правка требует это и text-only. DTMF не разрешает аудио. Отказ завершает разговор. Echo/настоящие устройства ещё не приёмочно проверены.
- `secure_store.py`: AES-GCM256/AAD UUID/company/kind, private0700/files0600, master32bytes/fingerprint guard; потерянный ключ нельзя заменять новым поверх архива.
- `archive.py`: encrypted PCM chunks, завершённый transcript encrypted JSON; после archive очищаются plaintext turns/context/memory/pending drafts. data_erased запрещает resurrection. Replay проверяет END footer/authentication. Capture OSError отключает запись с UI notification; finalize/recovery надо усилить.
- `archive_api.py`: metadata/text/audio/export permissions+обязательная цель, audit/no-store/retention. Delete tombstone, purge файлов позже. **Старый endpoint завершённой сессии способен читать legacy plaintext без цели — закрыть.**
- Развёртывание single-company id1, не multi-tenant. Активные поля PostgreSQL не зашифрованы данным архивным механизмом.
- `scripts/backup.py`: encrypted .vdbk вне repo (PG/CRM/.env/private adapters/archive/TLS), master key отдельно, лимит512MiB/in-memory. Два legacy plaintext backup проверены по SHA всех members и сохранены encrypted, дубли убраны. Проверяли authenticated restore main DB в отдельную voice_restore_test:9requests/9bookings/27outbox, production не трогали. Full CRM/adapter restore и persistent erasure ledger ещё нет. `.env` пока plaintext600 внутри проекта, не обещать все secrets вне iCloud.
- Миграции005–008 уже применялись; далее009+.

## ASR/NLU: цель не достигнута

Скрипты `download_eval_corpus.py`, `evaluate_public_speech.py`, `evaluate_public_intents.py` сохраняют версии/hash/параметры/ошибки. Настоящая публичная речь, помехи симулируются: phone8k/300–3400, SNR15dB улица/офис, echo0,12с/gain0,15, speed1,15, pauses0,35, combined. Phone G.711-style, не bit-exact codec. Нет подтверждённой speaker-disjoint разбивки/достаточной статистики. Это диагностика, не физическая приёмка устройств.

| Набор dev | Результат | Артефакт |
|---|---|---|
| MInDS14ru14×7 | слова66,9–77,1%; сомнительные/неполные references | `artifacts/asr-public-dev.json` |
| FLEURSru14×7,216слов/профиль | phone/fast93,9815%; street/office91,6667%; echo/pauses93,5185%; combined87,5% | `artifacts/asr-fleurs-whisper-dev.json` |
| MInDS NLU14 bank categories через ASR | phone13/14=92,857%; combined10/14=71,429% | `artifacts/nlu-public-dev-schema.json` |
| GigaAM RNNT FLEURS | **прервано71/98, полного итога нет** | `artifacts/asr-fleurs-gigaam-dev.json/.log` |

MInDS14:78 ru clips, CC-BY4, `PolyAI/minds14` revision `40ce77cb32a384e4d50a568e1ec39ac804019d33`; `~/.cache/voice-desk/evaluation/minds14-ru/manifest.json`. Банковская таксономия не проверяет booking/callback/handoff. Не менять сомнительный reference под output модели.

FLEURS: CC-BY4, `google/fleurs` revision `70bb2e84b976b7e960aa89f1c648e09c59f894dd`;42dev+42holdout, `~/.cache/voice-desk/evaluation/fleurs-ru/dev.json` и `holdout.json`. Holdout не оценивался, не подбирать по нему параметры.

GigaAM upstream `salute-developers/GigaAM` MIT; MLX port `aystream/gigaam-mlx` pinned `20276ddd6173d636b37c6c6e13b4ee8f7b94d1ac`, cache `evaluation/gigaam-code`. Candidate `aystream/GigaAM-v3-e2e-rnnt-mlx` revision `decfca492069ea30fb5ead79c4516d50c16d93ea`, `~/.cache/voice-desk/evaluation/gigaam-v3-rnnt`. Weights SHA `bbd1074b2466baf1a301b7a6c8427cea6d21a20d2ce1e173e7e447692619117f`; tokenizer SHA `828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a`. В текущую venv установлены gigaam-mlx0.1.0 из проверенного commit, librosa0.11.0, sentencepiece0.2.1. Это экспериментальные зависимости, setup/requirements ещё не обновлены. Production остаётся whisper.cpp large-v3-turbo q5 Metal. Не включать GigaAM без сравнения качества/скорости/RAM.

Повторить прерванный dev:

```sh
PYTHONPYCACHEPREFIX=/Users/andreevich/.cache/voice-desk/pycache .venv/bin/python scripts/evaluate_public_speech.py --backend gigaam-rnnt --model-dir /Users/andreevich/.cache/voice-desk/evaluation/gigaam-v3-rnnt --manifest /Users/andreevich/.cache/voice-desk/evaluation/fleurs-ru/dev.json --limit 14 --profiles phone street_simulated office_simulated echo fast pauses combined --output artifacts/asr-fleurs-gigaam-dev.json
```

Далее расширить dev, улучшать без подсматривания holdout, затем отдельная оценка holdout; продуктовому NLU нужен релевантный размеченный набор. Замена модели сама по себе не доказывает94%.

## Проверки и следующий приоритет

Предыдущий полный `artifacts/core-final-current.log`:73passed/4warnings до последних правок. После последних правок `artifacts/usage-consent-current.log`:4passed/2warnings; reproductions `call-usage-red.log`, `consent-notice-red.log`. Последний `npm run check` exit0. Нужны fresh full pytest/Ruff/browser.

Ранее:2 HTTPS voice+receipt (`core-voices-browser-20261013-retry.log`),5 design-workspace (`workspace-integration-current.log`),1 реальная FIFO RTC очередь (`call-queue-browser-current.log`). Это предыдущая версия, не приёмка последней. Первый HTTPS failure был сменой LANIP. Новые archive/CRM/audit и usage ещё не проверены в браузере.

1. Cold restart/ready/актуальныйLAN, regression, wide/mobile UI, RTC очередь/устный consent/negative recording.
2. Archive recovery/finalization/orphan files после cascade и erasure; policy retention каждого разговора вместо global30; legacy transcript purpose; memory/concurrency replay.
3. Consent таймер/перевод человеку до согласия/echo/storage failure; согласие и capture должны корректно отражать ошибки хранилища.
4. Business.confirm повторно проверять allowed_actions; regression factual guards; retry model load/DB chaos drill.
5. Runtime PostgreSQL user пока owner/superuser voice. HMAC без checkpoint/verifier не замечает удалённый хвост; missing username dummy KDF нет; multi-company архитектуры нет.
6. Persistent deletion ledger при старом restore, full CRM/adapter restore, key recovery.
7. Global model admission/priority нет, `_channel` модели фактически chat из-за отсутствующей voice_sessions map. IP20messages/min недостаточно для защиты всех ресурсов.
8. Telegram multi-call/обычный звонок и SIP реального провайдера не подтверждены; credentials пользователь отложил, секреты в чат не запрашивать.
9. Добиваться двух94% честными измерениями и обновить backlog/compliance/status; не снижать требования и не заявлять выпуск.

## Дополнение продолжения — 13 октября 2026: согласие, ядро и аудиоархив

Текущая точка — STATE.md/continue.md. Соседний чат продолжает выбор модели и оптимизацию задержки; этот блок не выбирал модели, не перезапускал его службы и не менял голоса. Изменены только необходимые пути сохранения/согласия/очистки, проверки и переход к защищённому архиву. Прежние незакоммиченные изменения сохранены.

- Business.draft/confirm проверяют согласие и состояние; confirm повторяет allowed_actions. HTTP/DTMF handoff использует единые ограничения.
- Dialogue повторно блокирует сессию перед записью контекста/реплик; поздний ответ после конца/erasure не возвращает данные. Память имени также не записывается в завершённую/удалённую сессию.
- В рабочей компании policy_version2: запись всех новых голосовых диалогов после полного предупреждения и устного разрешения. Consent deadline45с начинается после NoticeSent. До разрешения/при отказе нет аудиофайла. Записывается и сотрудник; ошибка хранилища сообщает отказ и завершает звонок.
- Старый endpoint завершённой сессии требует purpose и читает только защищённый архив. В кабинете завершённый разговор ведёт в раздел архива.
- Миграции009–012: отметка очистки, FK SET NULL вместо cascade, audio_status, отдельная очистка текста. Метаданные файлов переживают удаление session; очистка101 записей и повтор без работы проверены.
- Повторная финализация после сбоя SQL сохраняет закрытый capture; incomplete аудио не блокирует защищённую расшифровку и имеет собственный срок удаления. Восстановление повреждённых байтов/полной записи не заявляется.
- Retention использует policy snapshot и разные сроки текста/аудио, включая звонок с заявкой. Explicit erasure создаёт tombstone даже до появления архива, чтобы raw capture не оставался навсегда; это ещё не independent ledger для старых backup.
- Полный pytest210passed/35warnings; общий Ruff и JS check;9 UI browser на временной БД и390/1500px. Логи/manifest перечислены в STATE. UI test signal не является живым звонком/приёмкой94%.
- Новый encrypted backup и authenticated main DB restore в voice_restore_test проверены; полный CRM/adapter restore, RTO/RPO, checkpoint/DB ACL и old-backup erasure остаются открыты.

Содержимое документов знаний пока берётся из текущей опубликованной версии: snapshot knowledge_ids не равен полной фиксации содержимого на звонок. Telegram credentials пользователь отложил, реальный SIP-provider не выбран. Следующие миграции013+.

## Финальный повтор после восстановления окружения

После прерывания Docker daemon снова оказался остановлен: финальный фокусный прогон получил32 ошибки подготовки из-за отсутствующей PostgreSQL, до выполнения сценариев. OrbStack запущен через orbctl start, существующий db контейнер восстановлен docker compose up -d --wait db без сброса данных. Повтор:32 passed/2 warnings/11.80с (artifacts/security-focused-final.log). Общий Ruff и JS check прошли. Это дополнение не заменяет датированный полный210passed и9 UI tests; соседняя модельная работа продолжает меняться. Временный UI-server завершён с выполнением shutdown/удалением своей БД.
