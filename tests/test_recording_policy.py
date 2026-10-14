from uuid import UUID

from app.policy import enable_audio_recording, notice


async def test_enabling_recording_is_audited_and_applies_to_new_dialogues(business):
    old = await business.new_transport_session("sip", "male")
    before = await business.pool.fetchrow("SELECT settings,revision FROM company WHERE id=1")
    enabled = await enable_audio_recording(business.pool, actor="owner_request")
    assert enabled["settings"]["record_audio"]
    assert enabled["revision"] == before["revision"] + 1
    assert enabled["settings"]["policy_version"] == before["settings"]["policy_version"] + 1
    for source, profile in (("website", "male"), ("sip", "female"), ("telegram", "male")):
        new = await business.new_session("voice", False, profile) if source == "website" else await business.new_transport_session(source, profile)
        assert new["policy"]["record_audio"]
        assert "разрешаю запись разговора" in notice(new["policy"])
        assert not await business.pool.fetchval("SELECT audio_consent FROM sessions WHERE id=$1", UUID(new["id"]))
    assert not await business.pool.fetchval("SELECT (policy->>'record_audio')::bool FROM sessions WHERE id=$1", UUID(old["id"]))
    again = await enable_audio_recording(business.pool, actor="owner_request")
    assert again["revision"] == enabled["revision"]
    assert await business.pool.fetchval("SELECT count(*) FROM audit WHERE event='audio_recording_enabled'") == 1
