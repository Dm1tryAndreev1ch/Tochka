"""Incremental pitch-preserving speech tempo, including decoder tail flushing."""

import ctypes
import ctypes.util
from pathlib import Path

import numpy as np

_FLOAT = ctypes.POINTER(ctypes.c_float)
_CHANNELS = ctypes.POINTER(_FLOAT)


def _library():
    path = ctypes.util.find_library("rubberband")
    if not path:
        path = "/opt/homebrew/opt/rubberband/lib/librubberband.dylib"
    if not Path(path).exists() and "/" in path:
        raise RuntimeError("Speech tempo requires the Rubber Band library")
    lib = ctypes.CDLL(path)
    lib.rubberband_new.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_double,
    ]
    lib.rubberband_new.restype = ctypes.c_void_p
    for name in ("delete",):
        getattr(lib, "rubberband_" + name).argtypes = [ctypes.c_void_p]
    for name in ("get_preferred_start_pad", "get_start_delay"):
        fn = getattr(lib, "rubberband_" + name)
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_uint
    lib.rubberband_process.argtypes = [ctypes.c_void_p, _CHANNELS, ctypes.c_uint, ctypes.c_int]
    lib.rubberband_available.argtypes = [ctypes.c_void_p]
    lib.rubberband_available.restype = ctypes.c_int
    lib.rubberband_retrieve.argtypes = [ctypes.c_void_p, _CHANNELS, ctypes.c_uint]
    lib.rubberband_retrieve.restype = ctypes.c_uint
    return lib


class SpeechTempo:
    def __init__(self, rate=1.10, sample_rate=24000):
        if not 0.8 <= rate <= 1.25:
            raise ValueError("Speech rate is outside the supported range")
        self.lib = _library()
        # Real-time R3 speech engine, no background threads. Pitch stays 1.0.
        self.state = self.lib.rubberband_new(sample_rate, 1, 0x20010001, 1 / rate, 1.0)
        if not self.state:
            raise RuntimeError("Cannot initialize speech tempo")
        self.rate = rate
        self.input_samples = self.output_samples = 0
        self.skip = self.lib.rubberband_get_start_delay(self.state)
        pad = self.lib.rubberband_get_preferred_start_pad(self.state)
        self._process(np.zeros(pad, dtype=np.float32))

    def _process(self, audio, final=False):
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        channels = (_FLOAT * 1)(audio.ctypes.data_as(_FLOAT))
        self.lib.rubberband_process(self.state, channels, len(audio), int(final))

    def _read(self, final=False):
        chunks = []
        while (available := self.lib.rubberband_available(self.state)) > 0:
            audio = np.empty(available, dtype=np.float32)
            channels = (_FLOAT * 1)(audio.ctypes.data_as(_FLOAT))
            size = self.lib.rubberband_retrieve(self.state, channels, available)
            audio = audio[:size]
            trim = min(self.skip, len(audio))
            self.skip -= trim
            audio = audio[trim:]
            if final:
                remaining = max(0, round(self.input_samples / self.rate) - self.output_samples)
                audio = audio[:remaining]
            self.output_samples += len(audio)
            if audio.size:
                chunks.append(audio)
        return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)

    def push(self, audio):
        self.input_samples += len(audio)
        self._process(audio)
        return self._read()

    def finish(self):
        self._process(np.empty(0, dtype=np.float32), final=True)
        return self._read(final=True)

    def close(self):
        if self.state:
            self.lib.rubberband_delete(self.state)
            self.state = None
