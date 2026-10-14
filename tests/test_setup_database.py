import stat

from scripts.setup_database import with_credentials, write_database_url


def test_credentials_are_percent_encoded_and_location_is_kept():
    url = with_credentials("postgresql://voice:old@127.0.0.1:15432/voice", "voice_app", "p@ss/w:rd")
    assert url == "postgresql://voice_app:p%40ss%2Fw%3Ard@127.0.0.1:15432/voice"


def test_only_database_url_line_is_rewritten_and_mode_is_kept(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nDATABASE_URL=postgresql://voice:old@h/db\nB=$x # keep\n")
    env.chmod(0o600)
    write_database_url(env, "postgresql://voice_app:new@h/db")
    assert env.read_text() == "A=1\nDATABASE_URL=postgresql://voice_app:new@h/db\nB=$x # keep\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_database_url_is_appended_when_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1")
    write_database_url(env, "postgresql://u:p@h/db")
    assert env.read_text() == "A=1\nDATABASE_URL=postgresql://u:p@h/db\n"


def test_launcher_self_heals_only_the_compose_database_from_default_env(monkeypatch):
    from scripts import run

    default = run.ROOT / ".env"
    local = "postgresql://voice:x@127.0.0.1:15432/voice"
    assert run.auto_database_setup_allowed(local, default, {})
    assert not run.auto_database_setup_allowed(local, default, {"VOICE_AUTO_DB_SETUP": "0"})
    assert not run.auto_database_setup_allowed(local, default, {"DATABASE_URL": local})
    assert not run.auto_database_setup_allowed(local, run.ROOT / "other.env", {})
    assert not run.auto_database_setup_allowed(local, None, {})
    assert not run.auto_database_setup_allowed("postgresql://u:x@db.example.com:15432/voice", default, {})
    assert not run.auto_database_setup_allowed("postgresql://u:x@127.0.0.1:5432/voice", default, {})
