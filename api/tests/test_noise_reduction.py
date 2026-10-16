"""Tests for the per-agent noise_reduction workflow setting (RNNoise).

The filter is attached centrally, right after a transport factory returns,
so every telephony provider and browser calls get it without per-provider
code. These tests cover the setting, the helper, that the filter really
reaches pipecat's input transport, and that RNNoise works on 8 kHz phone
audio.
"""

import numpy as np
import pytest
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from api.schemas.workflow_configurations import WorkflowConfigurationDefaults
from api.services.pipecat.native_rnnoise_filter import NativeRNNoiseFilter
from api.services.pipecat.transport_params import (
    apply_noise_reduction,
    build_audio_in_filter,
)


# Native librnnoise where the bundled Linux build loads, pyrnnoise elsewhere.
RNNOISE_FILTERS = (NativeRNNoiseFilter, RNNoiseFilter)


class _FakeWebsocket:
    """Enough of a FastAPI WebSocket for the transport constructor."""

    client_state = None
    application_state = None


def _ws_transport():
    return FastAPIWebsocketTransport(
        websocket=_FakeWebsocket(),
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )


# ── Setting ───────────────────────────────────────────────────────────


def test_setting_defaults_off():
    assert WorkflowConfigurationDefaults().noise_reduction == "off"


def test_setting_accepts_rnnoise_and_rejects_unknown():
    assert (
        WorkflowConfigurationDefaults(noise_reduction="rnnoise").noise_reduction
        == "rnnoise"
    )
    with pytest.raises(Exception):
        WorkflowConfigurationDefaults(noise_reduction="krisp")


def test_stored_null_reads_as_off():
    assert (
        WorkflowConfigurationDefaults.model_validate(
            {"noise_reduction": None}
        ).noise_reduction
        == "off"
    )


# ── Helper ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [None, "", "off", "something-else"])
def test_no_filter_unless_rnnoise(mode):
    assert build_audio_in_filter(mode) is None


def test_rnnoise_gives_a_fresh_filter_per_call():
    a = build_audio_in_filter("rnnoise")
    b = build_audio_in_filter("rnnoise")
    assert isinstance(a, RNNOISE_FILTERS) and isinstance(b, RNNOISE_FILTERS)
    assert a is not b


@pytest.mark.parametrize("configs", [None, {}, {"noise_reduction": "off"}])
def test_apply_off_leaves_transport_untouched(configs):
    transport = _ws_transport()
    apply_noise_reduction(transport, configs, run_id=1)
    assert transport._params.audio_in_filter is None


def test_apply_rnnoise_reaches_the_input_transport():
    transport = _ws_transport()
    apply_noise_reduction(transport, {"noise_reduction": "rnnoise"}, run_id=1)
    # The input side is created lazily from the same params object, so the
    # filter set here is the one pipecat starts and runs every audio frame
    # through.
    assert isinstance(transport.input()._params.audio_in_filter, RNNOISE_FILTERS)


def test_apply_skips_a_transport_without_params():
    class NoParams:
        pass

    apply_noise_reduction(NoParams(), {"noise_reduction": "rnnoise"}, run_id=1)


def test_every_pipecat_transport_params_has_the_filter_slot():
    assert "audio_in_filter" in TransportParams.model_fields


# ── RNNoise on phone audio ────────────────────────────────────────────


def _pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def _rms(b):
    return float(np.sqrt(np.mean(np.frombuffer(b, np.int16).astype(np.float64) ** 2)))


async def _run_filter(signal, sample_rate=8000, chunk=160):
    f = build_audio_in_filter("rnnoise")
    await f.start(sample_rate)
    if isinstance(f, RNNoiseFilter):
        assert f._rnnoise_ready, "pyrnnoise did not load"
    pcm = _pcm(signal)
    out = bytearray()
    for i in range(0, len(pcm), chunk * 2):
        out += await f.filter(pcm[i : i + chunk * 2])
    await f.stop()
    return pcm, bytes(out)


@pytest.mark.asyncio
async def test_rnnoise_removes_background_noise_on_8khz_audio():
    rng = np.random.default_rng(0)
    noise = (0.08 * rng.standard_normal(8000 * 3)).astype(np.float32)
    pcm, out = await _run_filter(noise)
    assert _rms(out) < _rms(pcm) / 20  # more than 26 dB quieter


@pytest.mark.asyncio
async def test_rnnoise_keeps_speech_level_and_length():
    t = np.arange(8000 * 3) / 8000
    speech = (
        0.3 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 3 * t) > 0)
    ).astype(np.float32)
    rng = np.random.default_rng(1)
    noisy = speech + (0.08 * rng.standard_normal(len(t))).astype(np.float32)
    pcm, out = await _run_filter(noisy)
    # Audio keeps flowing at the same rate (no stall or big buffering delay).
    assert abs(len(out) - len(pcm)) <= 160 * 2 * 2
    # The voiced part survives: most of the clean speech energy remains.
    assert _rms(out) > 0.7 * _rms(_pcm(speech))


# ── Through pipecat's real input transport ───────────────────────────


class _InputOnlyTransport:
    """A transport shaped like every pipecat transport: one params object
    shared with its real BaseInputTransport."""

    def __init__(self, sample_rate):
        from pipecat.transports.base_input import BaseInputTransport

        class _ReadyInput(BaseInputTransport):
            # Real transports call set_transport_ready once connected; that
            # is what starts the audio loop that runs the filter.
            async def start(self, frame):
                await super().start(frame)
                await self.set_transport_ready(frame)

        self._params = TransportParams(
            audio_in_enabled=True, audio_in_sample_rate=sample_rate
        )
        self._input = _ReadyInput(self._params)

    def input(self):
        return self._input


async def _through_input_transport(mode, signal, sample_rate=8000, chunk=160):
    from pipecat.frames.frames import InputAudioRawFrame
    from pipecat.tests.utils import SleepFrame, run_test

    transport = _InputOnlyTransport(sample_rate)
    apply_noise_reduction(transport, {"noise_reduction": mode}, run_id=1)
    pcm = _pcm(signal)
    frames = [
        InputAudioRawFrame(
            audio=pcm[i : i + chunk * 2], sample_rate=sample_rate, num_channels=1
        )
        for i in range(0, len(pcm), chunk * 2)
    ]
    down, _ = await run_test(
        transport.input(),
        frames_to_send=[*frames, SleepFrame(0.5)],
        expected_down_frames=None,
    )
    out = b"".join(f.audio for f in down if isinstance(f, InputAudioRawFrame))
    return pcm, out, len(frames)


@pytest.mark.asyncio
async def test_input_transport_cleans_caller_audio_when_on():
    rng = np.random.default_rng(2)
    noise = (0.08 * rng.standard_normal(8000 * 2)).astype(np.float32)
    pcm, out, _ = await _through_input_transport("rnnoise", noise)
    assert len(out) >= len(pcm) - 160 * 2 * 2
    assert _rms(out) < _rms(pcm) / 20


@pytest.mark.asyncio
async def test_input_transport_leaves_audio_untouched_when_off():
    rng = np.random.default_rng(3)
    noise = (0.08 * rng.standard_normal(8000 * 2)).astype(np.float32)
    pcm, out, _ = await _through_input_transport("off", noise)
    assert out == pcm
