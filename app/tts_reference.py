"""Reuse speaker embeddings only for the immutable approved waveforms."""


def cache_reference_embeddings(model, references, evaluate):
    original = getattr(model, "extract_speaker_embedding", None)
    if original is None or getattr(model, "speaker_encoder", None) is None:
        return
    waveforms = [(reference["audio"], original(reference["audio"])) for reference in references.values()]
    evaluate(*(embedding for _, embedding in waveforms))

    def extract(audio):
        for waveform, embedding in waveforms:
            if audio is waveform:
                return embedding
        # Unrecognized audio still follows the library's normal behavior.
        return original(audio)

    model.extract_speaker_embedding = extract
