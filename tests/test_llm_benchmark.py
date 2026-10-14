"""Keep benchmark gates sensitive to real unsafe model regressions."""

from scripts.benchmark_llm_candidates import (
    MODELS,
    ROOT,
    components,
    evaluate,
    model_path,
    percentile,
    stream_probe,
    summary,
)


def test_missing_country_code_and_carried_name_are_not_accepted():
    failures = evaluate(
        {"expected": {"phone": None}, "absent": ["name"]},
        {"phone": "+375290000003", "name": "Клиент"},
        [],
    )
    assert len(failures) == 2


def test_phone_formatting_is_not_counted_as_a_quality_regression():
    assert evaluate(
        {"expected": {"phone": "+375290000001"}},
        {"phone": "+375 (29) 000-00-01"},
        [],
    ) == []


def test_irrelevant_quote_and_invented_quote_cannot_pass_grounding_gate():
    case = {"quote_contains": ["45"]}
    sources = ["Цена — 45 BYN. Гарантия — 30 дней."]
    assert evaluate(case, {"fact_quotes": ["Гарантия — 30 дней."]}, sources)
    assert evaluate(case, {"fact_quotes": ["Цена — 45 BYN, скидка 20%."]}, sources)
    assert evaluate(case, {"fact_quotes": ["Цена — 45 BYN."]}, sources) == []
    assert evaluate({"expected": {"fact_quotes": []}}, {"fact_quotes": []}, sources) == []


def test_schema_errors_still_count_toward_tail_latency_and_failure_rate():
    rows = [
        {"id": "ok", "group": "fields", "elapsed_seconds": 1, "output": {}, "failures": []},
        {"id": "timeout", "group": "fields", "elapsed_seconds": 35, "error_type": "AppError", "failures": ["schema error"]},
    ]
    result = summary(rows)
    assert result["cases"] == 2
    assert result["passed"] == result["valid_schema"] == 1
    assert result["elapsed_p95_seconds"] == 35
    assert percentile([], .95) is None


def test_schema_failure_remains_in_every_applicable_quality_denominator():
    case = {
        "expected": {"intent": "faq"},
        "absent": ["name"],
        "quote_contains": ["45"],
        "answer_required": True,
    }
    assert components(case, None, ["schema failed"]) == {
        "schema": False,
        "field:intent": False,
        "omit_unmentioned_fields": False,
        "grounding": False,
        "conversation_topic": False,
        "no_action_claim": False,
    }


async def test_stream_probe_detects_sentence_punctuation_before_closing_json_quote():
    import json

    import httpx

    message = {"choices": [{"delta": {"content": '{"intent":"conversation","answer":"Здравствуйте."}'}}]}
    payload = "data: " + json.dumps(message, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await stream_probe(client, "http://benchmark.test/completions", {})
    assert result["first_answer_sentence_seconds"] is not None
    assert result["content"] == message["choices"][0]["delta"]["content"]


def test_candidate_storage_is_explicit_and_baseline_stays_local(tmp_path, monkeypatch):
    import pytest

    monkeypatch.delenv("VOICE_TEST_MODEL_DIR", raising=False)
    assert model_path("qwen3-4b") == ROOT / MODELS["qwen3-4b"]
    with pytest.raises(ValueError, match="model-dir"):
        model_path("qwen3.5-2b")
    monkeypatch.setenv("VOICE_TEST_MODEL_DIR", str(tmp_path))
    assert model_path("qwen3.5-2b") == tmp_path / "qwen3.5-2b-q4.gguf"


def test_missing_external_destination_cannot_create_internal_download(tmp_path, monkeypatch):
    import pytest

    from scripts import download_models

    internal = tmp_path / "internal"
    monkeypatch.setattr(download_models, "DEST", internal)
    monkeypatch.setattr(download_models, "get_json", lambda url: pytest.fail("Network must not run for missing storage"))
    with pytest.raises(FileNotFoundError, match="unavailable"):
        download_models.download("example/repo", "revision", "model.gguf", "model.gguf", "Apache-2.0", destination=tmp_path / "missing")
    assert not internal.exists()


def test_existing_external_weight_is_verified_without_internal_redownload(tmp_path, monkeypatch):
    import hashlib

    from scripts import download_models

    external = tmp_path / "external"
    external.mkdir()
    payload = b"synthetic GGUF integrity evidence"
    target = external / "candidate.gguf"
    target.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(download_models, "DEST", tmp_path / "internal")
    monkeypatch.setattr(download_models, "get_json", lambda url: [{"path": "remote.gguf", "size": len(payload), "lfs": {"oid": sha}}])
    result = download_models.download("example/repo", "revision", "remote.gguf", target.name, "Apache-2.0", destination=external)
    assert result["sha256"] == sha
    assert result["path"] == str(target)
    assert not (tmp_path / "internal").exists()


def test_external_partial_symlink_cannot_redirect_writes_to_internal_storage(tmp_path, monkeypatch):
    import pytest

    from scripts import download_models

    external = tmp_path / "external"
    external.mkdir()
    internal = tmp_path / "internal-part"
    internal.write_bytes(b"preserve")
    (external / "candidate.gguf.part").symlink_to(internal)
    monkeypatch.setattr(download_models, "get_json", lambda url: pytest.fail("Network must not run for redirected storage"))
    with pytest.raises(ValueError, match="symlink"):
        download_models.download("example/repo", "revision", "remote.gguf", "candidate.gguf", "Apache-2.0", destination=external)
    assert internal.read_bytes() == b"preserve"
