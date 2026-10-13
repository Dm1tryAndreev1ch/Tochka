"""Small ctypes boundary around whisper.cpp, using the complete encoder context."""

import ctypes
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


class MetalWhisper:
    def __init__(self, model_path, use_gpu=True, compact_context=False):
        if not Path(model_path).is_file():
            raise FileNotFoundError("Whisper model is missing")
        self.lib = ctypes.CDLL(str(ROOT / ".runtime/libvoice_whisper.dylib"))
        self.lib.voice_whisper_create.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.lib.voice_whisper_create.restype = ctypes.c_void_p
        self.lib.voice_whisper_free.argtypes = [ctypes.c_void_p]
        self.lib.voice_whisper_free.restype = None
        self.lib.voice_whisper_version.argtypes = []
        self.lib.voice_whisper_version.restype = ctypes.c_char_p
        self.lib.voice_whisper_transcribe.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.lib.voice_whisper_transcribe.restype = ctypes.c_int
        self.compact_context = compact_context
        self.context = self.lib.voice_whisper_create(str(model_path).encode(), int(use_gpu))
        if not self.context:
            raise RuntimeError("Cannot initialize whisper.cpp")
        self.version = self.lib.voice_whisper_version().decode()

    def transcribe(self, samples):
        if not self.context:
            raise RuntimeError("Whisper context is closed")
        samples = np.ascontiguousarray(samples, dtype=np.float32)
        if samples.ndim != 1 or not np.isfinite(samples).all():
            raise ValueError("Whisper requires finite mono audio")
        if len(samples) < 160:
            return ""
        if len(samples) > 16000 * 21:
            raise ValueError("Whisper utterance exceeds 21 seconds")
        output = ctypes.create_string_buffer(8192)
        audio_context = max(256, int(len(samples) / 320) + 16) if self.compact_context else 0
        result = self.lib.voice_whisper_transcribe(
            self.context,
            samples.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            len(samples),
            output,
            len(output),
            audio_context,
        )
        if result:
            raise RuntimeError(f"Whisper inference failed ({result})")
        return output.value.decode("utf-8").strip()

    def close(self):
        if self.context:
            self.lib.voice_whisper_free(self.context)
            self.context = None
