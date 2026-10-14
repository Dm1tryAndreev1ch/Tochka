from types import SimpleNamespace

from app.tts_reference import cache_reference_embeddings


def test_approved_waveform_embeddings_are_reused_without_changing_unknown_audio():
    calls = []

    def extract(audio):
        calls.append(audio)
        return object()

    model = SimpleNamespace(speaker_encoder=object(), extract_speaker_embedding=extract)
    male, female, other = object(), object(), object()
    evaluated = []
    cache_reference_embeddings(model, {"male": {"audio": male}, "female": {"audio": female}}, lambda *items: evaluated.extend(items))
    assert len(calls) == 2 and len(evaluated) == 2
    assert model.extract_speaker_embedding(male) is evaluated[0]
    assert model.extract_speaker_embedding(female) is evaluated[1]
    assert len(calls) == 2
    assert model.extract_speaker_embedding(other) is not evaluated[0]
    assert calls[-1] is other and len(calls) == 3


def test_model_without_speaker_encoder_keeps_original_behavior():
    def extract(audio):
        return audio
    model = SimpleNamespace(speaker_encoder=None, extract_speaker_embedding=extract)
    cache_reference_embeddings(model, {}, lambda *items: None)
    assert model.extract_speaker_embedding is extract
