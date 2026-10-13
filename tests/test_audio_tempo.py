import numpy as np

from app.audio_tempo import SpeechTempo


def test_speed_preserves_pitch_and_flushes_final_sound():
    sample_rate = 24000
    body = 0.3 * np.sin(2 * np.pi * 220 * np.arange(sample_rate * 4) / sample_rate)
    ending = 0.3 * np.sin(2 * np.pi * 880 * np.arange(sample_rate // 5) / sample_rate)
    source = np.concatenate([body, ending]).astype(np.float32)
    worker = SpeechTempo(1.10)
    try:
        chunks = [worker.push(source[i : i + 7680]) for i in range(0, len(source), 7680)]
        chunks.append(worker.finish())
    finally:
        worker.close()
    audio = np.concatenate(chunks)
    assert abs(len(audio) - len(source) / 1.10) < sample_rate * 0.01
    for clip, expected in [(audio[sample_rate : sample_rate * 3], 220), (audio[-2400:], 880)]:
        spectrum = np.abs(np.fft.rfft(clip * np.hanning(len(clip))))
        pitch = np.fft.rfftfreq(len(clip), 1 / sample_rate)[spectrum.argmax()]
        assert abs(pitch - expected) <= 2
        assert np.sqrt(np.mean(clip**2)) > 0.1
