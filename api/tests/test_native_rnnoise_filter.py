"""Tests for the native-librnnoise caller noise filter.

The bundled librnnoise is a Linux x86_64 build, so the real-library tests
skip on other machines; the framing, resampling and lifecycle logic is
covered everywhere with a stand-in library.
"""

import ctypes

import numpy as np
import pytest
from pipecat.frames.frames import FilterEnableFrame

from api.services.pipecat import native_rnnoise_filter as native
from api.services.pipecat.native_rnnoise_filter import (
    RNNOISE_FRAME_SIZE,
    NativeRNNoiseFilter,
    RNNoiseCore,
    load_native_rnnoise,
)
from api.services.pipecat.transport_params import build_audio_in_filter


class _FakeLib:
    """Halves every sample, counts frames, and tracks create/destroy."""

    def __init__(self):
        self.frames = 0
        self.created = 0
        self.destroyed = 0

    def rnnoise_create(self, _model):
        self.created += 1
        return 1

    def rnnoise_destroy(self, _state):
        self.destroyed += 1

    def rnnoise_process_frame(self, _state, out_ptr, _in_ptr):
        self.frames += 1
        buf = np.ctypeslib.as_array(out_ptr, shape=(RNNOISE_FRAME_SIZE,))
        buf *= 0.5
        return 0.0


def _pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def _rms(b):
    return float(np.sqrt(np.mean(np.frombuffer(b, np.int16).astype(np.float64) ** 2)))


# ── Framing (any machine) ─────────────────────────────────────────────


def test_core_processes_whole_frames_and_holds_back_the_tail():
    lib = _FakeLib()
    core = RNNoiseCore(lib)
    # 700 samples = one full frame + a 220-sample tail held for later.
    out = core.process(np.full(700, 1000, np.int16).tobytes())
    assert len(out) == RNNOISE_FRAME_SIZE * 2 and lib.frames == 1
    assert np.all(np.frombuffer(out, np.int16) == 500)
    # 260 more samples complete the second frame with the held tail.
    out = core.process(np.full(260, 1000, np.int16).tobytes())
    assert len(out) == RNNOISE_FRAME_SIZE * 2 and lib.frames == 2


def test_core_returns_nothing_until_a_frame_is_full():
    lib = _FakeLib()
    core = RNNoiseCore(lib)
    assert core.process(np.zeros(100, np.int16).tobytes()) == b""
    assert lib.frames == 0


def test_core_clips_instead_of_wrapping():
    class Loud(_FakeLib):
        def rnnoise_process_frame(self, _s, out_ptr, _i):
            np.ctypeslib.as_array(out_ptr, shape=(RNNOISE_FRAME_SIZE,))[:] = 1e6
            return 0.0

    out = RNNoiseCore(Loud()).process(np.zeros(RNNOISE_FRAME_SIZE, np.int16).tobytes())
    assert np.all(np.frombuffer(out, np.int16) == 32767)


def test_core_close_destroys_state_once():
    lib = _FakeLib()
    core = RNNoiseCore(lib)
    core.close()
    core.close()
    assert lib.created == 1 and lib.destroyed == 1


@pytest.mark.asyncio
async def test_filter_keeps_8khz_audio_flowing_at_the_same_rate():
    f = NativeRNNoiseFilter(_FakeLib())
    await f.start(8000)
    pcm = np.full(8000 * 2, 8000, np.int16).tobytes()  # 2 s of 8 kHz audio
    out = bytearray()
    for i in range(0, len(pcm), 320):  # 20 ms phone frames
        out += await f.filter(pcm[i : i + 320])
    await f.stop()
    assert abs(len(out) - len(pcm)) <= 160 * 2 * 2
    assert _rms(bytes(out)) < _rms(pcm)  # the stand-in halved it


@pytest.mark.asyncio
async def test_filter_passes_audio_through_when_disabled():
    f = NativeRNNoiseFilter(_FakeLib())
    await f.start(8000)
    await f.process_frame(FilterEnableFrame(enable=False))
    chunk = np.full(160, 1234, np.int16).tobytes()
    assert await f.filter(chunk) == chunk


@pytest.mark.asyncio
async def test_filter_stop_releases_the_native_state():
    lib = _FakeLib()
    f = NativeRNNoiseFilter(lib)
    await f.start(16000)
    await f.stop()
    assert lib.destroyed == 1
    chunk = np.zeros(320, np.int16).tobytes()
    assert await f.filter(chunk) == chunk  # stopped: no crash, passthrough


def test_missing_library_falls_back_to_pyrnnoise(monkeypatch, tmp_path):
    from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

    monkeypatch.setattr(native, "_library", None)
    monkeypatch.setattr(native, "_library_error", None)
    monkeypatch.setenv("RNNOISE_LIBRARY_PATH", str(tmp_path / "missing.so"))
    assert load_native_rnnoise() is None
    assert isinstance(build_audio_in_filter("rnnoise"), RNNoiseFilter)


# ── The real bundled library (Linux x86_64 only) ─────────────────────

_real = None
try:
    _real = ctypes.CDLL(str(native.DEFAULT_LIBRARY_PATH))
except OSError:
    pass
needs_native = pytest.mark.skipif(
    _real is None, reason="bundled librnnoise only loads on Linux x86_64"
)


async def _run_real(signal, sample_rate=8000):
    lib = load_native_rnnoise()
    assert lib is not None
    f = NativeRNNoiseFilter(lib)
    await f.start(sample_rate)
    pcm = _pcm(signal)
    out = bytearray()
    for i in range(0, len(pcm), 320):
        out += await f.filter(pcm[i : i + 320])
    await f.stop()
    return pcm, bytes(out)


@needs_native
def test_build_audio_in_filter_uses_the_native_library_when_it_loads():
    assert isinstance(build_audio_in_filter("rnnoise"), NativeRNNoiseFilter)


@needs_native
@pytest.mark.asyncio
async def test_native_removes_background_noise_on_8khz_audio():
    noise = (0.08 * np.random.default_rng(0).standard_normal(8000 * 3)).astype(np.float32)
    pcm, out = await _run_real(noise)
    assert _rms(out) < _rms(pcm) / 20  # more than 26 dB quieter


@needs_native
@pytest.mark.asyncio
async def test_native_keeps_speech_level_and_length():
    t = np.arange(8000 * 3) / 8000
    speech = (0.3 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 3 * t) > 0)).astype(
        np.float32
    )
    noisy = speech + (0.08 * np.random.default_rng(1).standard_normal(len(t))).astype(np.float32)
    pcm, out = await _run_real(noisy)
    assert abs(len(out) - len(pcm)) <= 160 * 2 * 2
    assert _rms(out) > 0.7 * _rms(_pcm(speech))
