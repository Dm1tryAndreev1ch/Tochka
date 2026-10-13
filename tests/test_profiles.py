from app.config import settings


async def test_voice_profile_is_per_session_and_validated(client):
    response = await client.post(
        "/api/v1/sessions", json={"channel": "chat", "consent": True, "voice_profile": "female"}
    )
    assert response.status_code == 201
    assert response.json()["voice_profile"] == "female"
    invalid = await client.post(
        "/api/v1/sessions", json={"channel": "chat", "consent": True, "voice_profile": "../../private"}
    )
    assert invalid.status_code == 422
    profiles = (await client.get("/api/v1/voice-profiles")).json()["items"]
    assert [r["id"] for r in profiles] == ["male", "female"]


async def test_https_operator_cookie_is_secure(client):
    response = await client.post(
        "/api/v1/operator/login",
        json={"password": settings.admin_password},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
