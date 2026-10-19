"""Ambient sound play modes: saved through the real API, read by real transports.

1. The new fields save with PUT /workflow/{id} and read back intact, and the
   call path builds a mixer that plays only at moments from them.
2. Bad values are rejected (422) and nothing is stored.
3. The real transport classes (every phone provider's
   FastAPIWebsocketTransport, the browser's SmallWebRTCTransport) carry the
   mixer where the gate looks for it.
4. A custom uploaded file follows the play mode too.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.frames.frames import FunctionCallFromLLM, FunctionCallsStartedFrame
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from api.constants import APP_ROOT_DIR
from api.services.pipecat.ambient_sound import (
    AmbientSoundGate,
    GatedSoundfileMixer,
    attach_ambient_sound_gate,
)
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.audio_mixer import build_audio_out_mixer
from api.services.pipecat.transport_setup import create_webrtc_transport
from api.tests.integrations._run_pipeline_helpers import create_workflow_run_rows

from .test_greeting_and_noise_settings_api import _call
from .test_run_pipeline_greeting_interrupt import _definition

AMBIENT = {
    "enabled": True,
    "volume": 0.4,
    "play_mode": "tool_calls",
    "start_delay_secs": 0.8,
    "sound": "typing",
}


@pytest.mark.asyncio
async def test_play_mode_settings_save_and_drive_the_call_mixer(db_session, async_session):
    _, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(False),
        name_prefix="Ambient settings API",
        provider_id_suffix="ambient-settings-api",
    )
    resp = await _call(
        user, "PUT", f"/workflow/{workflow.id}", json={"workflow_configurations": {"ambient_noise_configuration": AMBIENT}}
    )
    assert resp.status_code == 200, resp.text

    got = await _call(user, "GET", f"/workflow/fetch/{workflow.id}")
    saved = got.json()["workflow_configurations"]["ambient_noise_configuration"]
    assert {k: saved[k] for k in AMBIENT} == AMBIENT

    # The call path builds its mixer from exactly this stored config.
    mixer = await build_audio_out_mixer(8000, saved)
    assert isinstance(mixer, GatedSoundfileMixer)
    await mixer.start(8000)
    assert "typing" in mixer._sounds


@pytest.mark.asyncio
async def test_saving_without_the_new_fields_keeps_whole_call(db_session, async_session):
    _, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(False),
        name_prefix="Ambient settings legacy",
        provider_id_suffix="ambient-settings-legacy",
    )
    legacy = {"enabled": True, "volume": 0.3}  # what older clients send
    resp = await _call(
        user, "PUT", f"/workflow/{workflow.id}", json={"workflow_configurations": {"ambient_noise_configuration": legacy}}
    )
    assert resp.status_code == 200, resp.text
    got = await _call(user, "GET", f"/workflow/fetch/{workflow.id}")
    saved = got.json()["workflow_configurations"]["ambient_noise_configuration"]
    assert type(await build_audio_out_mixer(8000, saved)) is SoundfileMixer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"play_mode": "sometimes"},
        {"play_mode": "TOOL_CALLS"},
        {"start_delay_secs": -1},
        {"start_delay_secs": 9},
        {"sound": "jungle"},
    ],
    ids=["mode", "mode-case", "delay-negative", "delay-too-long", "sound"],
)
async def test_bad_play_mode_settings_are_rejected(db_session, async_session, bad):
    _, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(False),
        name_prefix="Ambient settings bad",
        provider_id_suffix=f"ambient-settings-bad-{'-'.join(map(str, bad.values()))}",
    )
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={"workflow_configurations": {"ambient_noise_configuration": {"enabled": True, **bad}}},
    )
    assert resp.status_code == 422, resp.text
    got = await _call(user, "GET", f"/workflow/fetch/{workflow.id}")
    stored = (got.json().get("workflow_configurations") or {}).get("ambient_noise_configuration") or {}
    assert not set(bad) & set(stored)


# ── The real transport classes ────────────────────────────────────────


async def _assert_gate_follows(transport, mixer):
    added = []
    task = MagicMock()
    task.add_observer.side_effect = added.append
    attach_ambient_sound_gate(task, transport, AMBIENT)
    assert len(added) == 1 and isinstance(added[0], AmbientSoundGate)
    assert added[0]._mixer is mixer


@pytest.mark.asyncio
async def test_phone_transport_carries_the_mixer_where_the_gate_finds_it():
    mixer = await build_audio_out_mixer(8000, AMBIENT)
    transport = FastAPIWebsocketTransport(
        websocket=MagicMock(),
        params=FastAPIWebsocketParams(audio_out_enabled=True, audio_out_sample_rate=8000, audio_out_mixer=mixer),
    )
    await _assert_gate_follows(transport, mixer)


@pytest.mark.asyncio
async def test_browser_transport_carries_the_mixer_where_the_gate_finds_it():
    connection = MagicMock()
    connection.event_handler = lambda name: (lambda fn: fn)
    transport = await create_webrtc_transport(
        connection, workflow_run_id=1, audio_config=create_audio_config("smallwebrtc"), ambient_noise_config=AMBIENT
    )
    mixer = transport._params.audio_out_mixer
    assert isinstance(mixer, GatedSoundfileMixer)
    await _assert_gate_follows(transport, mixer)


# ── Custom uploaded sound ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_custom_uploaded_sound_follows_the_play_mode():
    uploaded = str(APP_ROOT_DIR / "assets" / "office-ambience-8000-mono.wav")  # stands in for the cached upload
    cfg = {**AMBIENT, "start_delay_secs": 0.0, "storage_key": "ambient/org5/hold.wav", "storage_backend": "minio"}
    with patch("api.services.pipecat.audio_mixer.get_cached_ambient_noise_path", AsyncMock(return_value=uploaded)):
        mixer = await build_audio_out_mixer(8000, cfg)
    assert isinstance(mixer, GatedSoundfileMixer)
    await mixer.start(8000)
    assert mixer._current_sound == "custom" and "custom" in mixer._sounds

    silence = b"\x00" * 320
    assert await mixer.mix(silence) == silence  # silent until a tool runs
    gate = AmbientSoundGate(mixer, mode="tool_calls", start_delay_secs=0.0)
    call = FunctionCallFromLLM(function_name="lookup", tool_call_id="c1", arguments={}, context=None)
    await gate.on_push_frame(SimpleNamespace(frame=FunctionCallsStartedFrame(function_calls=[call])))
    await asyncio.sleep(0.02)
    assert gate.playing
    heard = [await mixer.mix(silence) for _ in range(20)]
    assert any(chunk != silence for chunk in heard)
