"""LAN certificate onboarding, with only three explicitly allowed public artifacts."""

import html
import json

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import ROOT

config = json.loads((ROOT / ".runtime/mobile.json").read_text())
public = ROOT / ".runtime/mobile-public"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[config["ip"], "127.0.0.1", "localhost"])


@app.get("/", response_class=HTMLResponse)
def home():
    url = html.escape(config["url"])
    fingerprint = html.escape(config["ca_sha256"])
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Точка · телефон</title><style>body{{background:#f5f4ee;color:#252923;font:16px/1.65 sans-serif;margin:0}}main{{max-width:660px;margin:24px auto;padding:24px}}section{{background:#fffef9;border:1px solid #dce1d2;border-radius:14px;padding:22px;margin:20px 0}}h1{{font-size:34px;line-height:1.2}}a{{color:#526632}}.button{{display:block;background:#d3e78d;border-radius:9px;padding:14px;text-align:center;text-decoration:none;color:#252923}}code{{font-size:12px;overflow-wrap:anywhere}}img{{width:140px;display:block;margin:auto}}</style></head><body><main><p>ТОЧКА / ТЕЛЕФОН</p><h1>Одна сеть.<br>Один хороший разговор.</h1><p>Телефон и Mac должны быть в одной Wi-Fi сети. Сертификат устанавливается один раз, чтобы браузер разрешил защищённый доступ к микрофону.</p>
<section><h2>iPhone · Safari</h2><ol><li><a href="/voice-desk.mobileconfig">Скачайте профиль «Точка Wi-Fi»</a>.</li><li>Настройки → Основные → VPN и управление устройством → скачанный профиль → Установить.</li><li>Настройки → Основные → Об этом устройстве → Доверие сертификатам. Включите доверие локальному сертификату Точки.</li><li>Откройте приложение кнопкой ниже и разрешите микрофон.</li></ol><a href="https://support.apple.com/en-ie/102390" rel="noreferrer">Инструкция Apple</a></section>
<section><h2>Android · Chrome</h2><ol><li><a href="/voice-desk-ca.cer">Скачайте сертификат Точки</a>.</li><li>Настройки → Безопасность → Дополнительные настройки → Шифрование и учётные данные → Установить сертификат → Сертификат CA. Названия зависят от телефона.</li><li>Выберите voice-desk-ca.cer и подтвердите установку.</li><li>Откройте приложение в Chrome и разрешите микрофон.</li></ol><a href="https://support.google.com/pixelphone/answer/2844832" rel="noreferrer">Инструкция Google</a></section>
<section><h2>Сверка сертификата</h2><p>Сравните SHA-256 со значением на Mac:</p><code>{fingerprint}</code><p>Приватный ключ не передаётся на телефон. После тестов профиль можно удалить в настройках телефона.</p></section><a class="button" href="{url}">Открыть помощника →</a><p><a href="{url}">{url}</a></p><p>Если страница не открывается, проверьте Wi-Fi, гостевую изоляцию и разрешение локальной сети. При запросе macOS разрешите входящее соединение приложения. Mac должен работать.</p><img src="/setup-qr.svg" alt="QR-код этой инструкции"></main></body></html>'''


@app.get("/voice-desk-ca.cer")
def certificate():
    return FileResponse(
        public / "voice-desk-ca.cer", media_type="application/x-x509-ca-cert", filename="voice-desk-ca.cer"
    )


@app.get("/voice-desk.mobileconfig")
def profile():
    return FileResponse(
        public / "voice-desk.mobileconfig",
        media_type="application/x-apple-aspen-config",
        filename="voice-desk.mobileconfig",
    )


@app.get("/setup-qr.svg")
def qr():
    return FileResponse(public / "setup-qr.svg", media_type="image/svg+xml")
