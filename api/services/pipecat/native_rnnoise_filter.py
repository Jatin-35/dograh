"""Caller-side RNNoise noise suppression on the bundled native library.

pipecat's ``RNNoiseFilter`` goes through ``pyrnnoise``, whose bundled
librnnoise has no SIMD code paths and which pushes every chunk through a PyAV
audio graph. On Linux that costs ~11% of a core per call. The librnnoise that
ships in ``api/native/rnnoise`` is built with runtime CPU dispatch (AVX/AVX2)
and costs ~3.7% for the same audio, so this filter calls it directly through
ctypes and does its own 10 ms framing.

RNNoise only runs at 48 kHz, so 8/16 kHz call audio is resampled up and back
down around it (the resampling itself is ~0.1% of a core).
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import numpy as np
from loguru import logger

from api.constants import APP_ROOT_DIR
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame

RNNOISE_SAMPLE_RATE = 48000
RNNOISE_FRAME_SIZE = 480  # 10 ms at 48 kHz, fixed by the library
DEFAULT_LIBRARY_PATH = APP_ROOT_DIR / "native" / "rnnoise" / "librnnoise.so.0.4.1"

_library: ctypes.CDLL | None = None
_library_error: str | None = None


def _library_path() -> Path:
    return Path(os.environ.get("RNNOISE_LIBRARY_PATH") or DEFAULT_LIBRARY_PATH)


def load_native_rnnoise() -> ctypes.CDLL | None:
    """The bundled librnnoise, loaded once per process; None if unavailable.

    It is a Linux x86_64 build, so on a developer's Windows or macOS machine
    this returns None and the caller falls back to pipecat's filter.
    """
    global _library, _library_error
    if _library is not None or _library_error is not None:
        return _library
    path = _library_path()
    try:
        lib = ctypes.CDLL(str(path))
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        frame_size = lib.rnnoise_get_frame_size()
        if frame_size != RNNOISE_FRAME_SIZE:
            raise OSError(f"unexpected RNNoise frame size {frame_size}")
    except (OSError, AttributeError) as e:
        _library_error = f"{path}: {e}"
        logger.info(f"Native RNNoise unavailable ({_library_error})")
        return None
    _library = lib
    logger.info(f"Native RNNoise loaded from {path}")
    return _library


class RNNoiseCore:
    """One stream's RNNoise state, working on 48 kHz int16 PCM.

    Holds back any tail shorter than one 10 ms frame until the next call, so
    output length tracks input length to within a frame.
    """

    def __init__(self, lib: ctypes.CDLL):
        self._lib = lib
        self._state = lib.rnnoise_create(None)
        if not self._state:
            raise MemoryError("rnnoise_create failed")
        self._pending = np.zeros(0, dtype=np.float32)
        self._frame = np.zeros(RNNOISE_FRAME_SIZE, dtype=np.float32)
        self._frame_ptr = self._frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

    def process(self, pcm48: bytes) -> bytes:
        samples = np.frombuffer(pcm48, dtype=np.int16).astype(np.float32)
        if self._pending.size:
            samples = np.concatenate((self._pending, samples))
        frames = samples.size // RNNOISE_FRAME_SIZE
        usable = frames * RNNOISE_FRAME_SIZE
        self._pending = samples[usable:].copy()
        if frames == 0:
            return b""
        out = np.empty(usable, dtype=np.float32)
        frame, ptr, process = self._frame, self._frame_ptr, self._lib.rnnoise_process_frame
        for start in range(0, usable, RNNOISE_FRAME_SIZE):
            end = start + RNNOISE_FRAME_SIZE
            frame[:] = samples[start:end]
            process(self._state, ptr, ptr)  # in place; RNNoise expects int16 scale
            out[start:end] = frame
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()

    def close(self) -> None:
        if self._state:
            self._lib.rnnoise_destroy(self._state)
            self._state = None


class NativeRNNoiseFilter(BaseAudioFilter):
    """pipecat audio filter running the bundled librnnoise on caller audio."""

    def __init__(self, lib: ctypes.CDLL):
        self._lib = lib
        self._core: RNNoiseCore | None = None
        self._filtering = True
        self._sample_rate = 0
        self._resampler_in = None
        self._resampler_out = None

    async def start(self, sample_rate: int):
        self._sample_rate = sample_rate
        self._core = RNNoiseCore(self._lib)
        if sample_rate != RNNOISE_SAMPLE_RATE:
            from pipecat.audio.resamplers.soxr_stream_resampler import (
                SOXRStreamAudioResampler,
            )

            self._resampler_in = SOXRStreamAudioResampler(quality="QQ")
            self._resampler_out = SOXRStreamAudioResampler(quality="QQ")

    async def stop(self):
        if self._core is not None:
            self._core.close()
            self._core = None
        self._resampler_in = None
        self._resampler_out = None

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            self._filtering = frame.enable

    async def filter(self, audio: bytes) -> bytes:
        if not self._filtering or self._core is None or not audio:
            return audio
        pcm48 = audio
        if self._resampler_in is not None:
            pcm48 = await self._resampler_in.resample(
                audio, self._sample_rate, RNNOISE_SAMPLE_RATE
            )
        cleaned = self._core.process(pcm48)
        if not cleaned:
            return b""
        if self._resampler_out is not None:
            return await self._resampler_out.resample(
                cleaned, RNNOISE_SAMPLE_RATE, self._sample_rate
            )
        return cleaned
