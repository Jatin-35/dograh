"""Dograh-driven turns for Inworld Realtime (turn detection "dograh_local").

Inworld's own turn detection is switched off (manual mode) and Dograh decides
turns. Covers the word-gated start rule, how a call wires it up, and the
wrapper's manual-mode behaviour: only the caller's speech reaches Inworld,
commits happen only with speech buffered, and a real interruption stops the
reply without cutting off the caller's first words. No network: every socket
here is fake.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.services.inworld.realtime import events
from pipecat.services.inworld.realtime import llm as inworld_llm
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.external_user_turn_start_strategy import (
    ExternalUserTurnStartStrategy,
)

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.registry import (
    InworldRealtimeLLMConfiguration,
    ServiceProviders,
)
from api.services.pipecat.realtime.inworld_realtime import (
    DograhInworldRealtimeLLMService,
)
from api.services.pipecat.realtime.local_turns import (
    WordGatedVADUserTurnStartStrategy,
    dograh_local_turn_config,
)
from api.services.pipecat.run_pipeline import _create_realtime_user_turn_config
from api.services.pipecat.service_factory import create_realtime_llm_service
from api.services.pipecat.turn_stop_strategies import (
    DograhSpeechTimeoutUserTurnStopStrategy,
)

RATE = 8000
FRAME_20MS = b"\x01\x00" * (RATE // 50)  # 20 ms of 16-bit mono


def _audio(n_frames=1):
    return [InputAudioRawFrame(audio=FRAME_20MS, sample_rate=RATE, num_channels=1) for _ in range(n_frames)]


def _strategy(**kwargs):
    s = WordGatedVADUserTurnStartStrategy(**kwargs)
    s.trigger_user_turn_started = AsyncMock()
    return s, None


async def _speak(s, secs):
    """Feed `secs` of audio frames (20 ms each); returns the last result."""
    result = ProcessFrameResult.CONTINUE
    for frame in _audio(round(secs * 50)):
        result = await s.process_frame(frame)
    return result


# ── The word-gated start rule ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_quiet_the_turn_starts_on_voice():
    s, _ = _strategy()
    assert await s.process_frame(VADUserStartedSpeakingFrame()) == ProcessFrameResult.STOP
    s.trigger_user_turn_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_speaking_voice_alone_does_not_interrupt():
    s, _ = _strategy()
    await s.process_frame(BotStartedSpeakingFrame())
    assert await s.process_frame(VADUserStartedSpeakingFrame()) == ProcessFrameResult.CONTINUE
    for frame in _audio(10):
        await s.process_frame(frame)
    s.trigger_user_turn_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_speaking_one_word_is_not_enough_two_words_interrupt():
    s, _ = _strategy()
    await s.process_frame(BotStartedSpeakingFrame())
    await s.process_frame(VADUserStartedSpeakingFrame())
    await s.process_frame(InterimTranscriptionFrame("haan", "", "t"))
    s.trigger_user_turn_started.assert_not_awaited()
    assert await s.process_frame(InterimTranscriptionFrame("haan ruko", "", "t")) == ProcessFrameResult.STOP
    s.trigger_user_turn_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_min_words_setting_is_respected():
    s, _ = _strategy(min_words=3)
    await s.process_frame(BotStartedSpeakingFrame())
    await s.process_frame(VADUserStartedSpeakingFrame())
    await s.process_frame(InterimTranscriptionFrame("ek minute", "", "t"))
    s.trigger_user_turn_started.assert_not_awaited()
    await s.process_frame(InterimTranscriptionFrame("ek minute ruko", "", "t"))
    s.trigger_user_turn_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_sustained_speech_interrupts_when_no_transcript_comes():
    s, _ = _strategy(min_barge_in_secs=1.2)
    await s.process_frame(BotStartedSpeakingFrame())
    await s.process_frame(VADUserStartedSpeakingFrame())
    await _speak(s, 1.1)
    s.trigger_user_turn_started.assert_not_awaited()
    assert await _speak(s, 0.1) == ProcessFrameResult.STOP
    s.trigger_user_turn_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_speech_with_short_pauses_still_adds_up_to_an_interruption():
    # Real speech pauses between words ("Main Jaipur, | Rajasthan se hoon"):
    # VAD stops briefly, but the caller is still talking over the bot.
    s, _ = _strategy(min_barge_in_secs=1.2)
    await s.process_frame(BotStartedSpeakingFrame())
    for _ in range(3):
        await s.process_frame(VADUserStartedSpeakingFrame())
        await _speak(s, 0.5)
        await s.process_frame(VADUserStoppedSpeakingFrame())
        await _speak(s, 0.3)  # a pause shorter than the gap
    s.trigger_user_turn_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_short_blip_while_the_bot_speaks_never_interrupts():
    s, _ = _strategy(min_barge_in_secs=1.2)
    await s.process_frame(BotStartedSpeakingFrame())
    await s.process_frame(VADUserStartedSpeakingFrame())
    await _speak(s, 0.4)
    await s.process_frame(VADUserStoppedSpeakingFrame())
    await _speak(s, 5)
    s.trigger_user_turn_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_blips_separated_by_real_silence_never_add_up():
    s, _ = _strategy(min_barge_in_secs=1.2)
    await s.process_frame(BotStartedSpeakingFrame())
    for _ in range(4):  # 4 x 0.5 s of noise, each followed by 0.8 s of quiet
        await s.process_frame(VADUserStartedSpeakingFrame())
        await _speak(s, 0.5)
        await s.process_frame(VADUserStoppedSpeakingFrame())
        await _speak(s, 0.8)
    s.trigger_user_turn_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_late_final_transcript_does_not_open_a_phantom_turn():
    # The final transcript arrives after the turn ended (on commit), while the
    # caller is silent; counting it would cancel the reply just asked for.
    s, _ = _strategy()
    await s.process_frame(TranscriptionFrame("ji madam bilkul sahi hai", "", "t"))
    s.trigger_user_turn_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_the_bot_stops_one_word_or_voice_starts_the_turn_again():
    s, _ = _strategy()
    await s.process_frame(BotStartedSpeakingFrame())
    await s.process_frame(BotStoppedSpeakingFrame())
    await s.process_frame(VADUserStartedSpeakingFrame())
    s.trigger_user_turn_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_keeps_knowing_the_bot_is_speaking():
    s, _ = _strategy()
    await s.process_frame(BotStartedSpeakingFrame())
    await s.reset()
    await s.process_frame(VADUserStartedSpeakingFrame())
    s.trigger_user_turn_started.assert_not_awaited()


# ── How a call wires it up ────────────────────────────────────────────


def test_local_turn_config_uses_the_word_gated_rule_and_silence_timeout():
    strategies, vad = dograh_local_turn_config(min_words=3, min_barge_in_secs=2.0, silence_secs=0.9)
    (start,) = strategies.start
    (stop,) = strategies.stop
    assert isinstance(start, WordGatedVADUserTurnStartStrategy)
    assert start._min_words == 3 and start._min_barge_in_secs == 2.0
    assert isinstance(stop, DograhSpeechTimeoutUserTurnStopStrategy)
    assert stop._user_speech_timeout == 0.9
    assert stop._wait_for_transcript is False
    assert vad is not None


def test_unset_local_settings_use_the_defaults():
    strategies, _ = dograh_local_turn_config()
    assert strategies.start[0]._min_words == 2
    assert strategies.start[0]._min_barge_in_secs == 1.2
    assert strategies.stop[0]._user_speech_timeout == 0.6


def test_an_inworld_agent_on_dograh_local_gets_dograh_turns():
    cfg = InworldRealtimeLLMConfiguration(
        api_key="k", turn_detection="dograh_local", local_min_words=3, local_silence_secs=0.8
    )
    strategies, vad = _create_realtime_user_turn_config(ServiceProviders.INWORLD_REALTIME.value, cfg)
    assert isinstance(strategies.start[0], WordGatedVADUserTurnStartStrategy)
    assert strategies.start[0]._min_words == 3
    assert strategies.stop[0]._user_speech_timeout == 0.8
    assert vad is not None


@pytest.mark.parametrize("mode", ["semantic_vad", "server_vad"])
def test_other_inworld_modes_keep_following_inworld(mode):
    cfg = InworldRealtimeLLMConfiguration(api_key="k", turn_detection=mode)
    strategies, vad = _create_realtime_user_turn_config(ServiceProviders.INWORLD_REALTIME.value, cfg)
    assert isinstance(strategies.start[0], ExternalUserTurnStartStrategy)
    assert vad is None


def test_callers_that_pass_no_config_are_unchanged():
    strategies, vad = _create_realtime_user_turn_config(ServiceProviders.INWORLD_REALTIME.value)
    assert isinstance(strategies.start[0], ExternalUserTurnStartStrategy)
    assert vad is None


def test_dograh_local_settings_show_only_in_that_mode():
    props = InworldRealtimeLLMConfiguration.model_json_schema()["properties"]
    assert "dograh_local" in props["turn_detection"]["enum"]
    for field in ("local_min_words", "local_silence_secs", "local_barge_in_secs"):
        assert props[field]["visible_when"] == {"stt_tts_source": "inworld", "turn_detection": "dograh_local"}


@pytest.mark.parametrize("field,value", [
    ("local_min_words", 0), ("local_min_words", 7),
    ("local_silence_secs", 0.1), ("local_silence_secs", 4),
    ("local_barge_in_secs", 0.2), ("local_barge_in_secs", 6),
])
def test_local_settings_reject_bad_values(field, value):
    with pytest.raises(ValueError):
        InworldRealtimeLLMConfiguration(api_key="k", **{field: value})


# ── The wrapper in manual mode ────────────────────────────────────────


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    def types(self):
        return [m["type"] for m in self.sent]


def _local_service(**kwargs):
    service = DograhInworldRealtimeLLMService(api_key="k", turn_detection="dograh_local", **kwargs)
    service._llm_needs_conversation_setup = False
    sock = _FakeSocket()
    service._websocket = sock
    service.push_frame = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()
    service.start_processing_metrics = AsyncMock()
    service.stop_all_metrics = AsyncMock()
    return service, sock


async def _feed_audio(service, n):
    for frame in _audio(n):
        await service._send_user_audio(frame)


def test_manual_mode_is_on_inside_the_service():
    service, _ = _local_service()
    assert service._is_manual_turn_detection() is True


@pytest.mark.asyncio
async def test_session_sends_an_explicit_null_turn_detection():
    service, sock = _local_service(speech_threshold=0.6, eagerness="high")
    await service._send_session_update()
    audio_in = sock.sent[0]["session"]["audio"]["input"]
    assert "turn_detection" in audio_in and audio_in["turn_detection"] is None
    # Inworld-STT turn tuning doesn't apply when Dograh decides turns.
    assert "stt" not in sock.sent[0]["session"]["providerData"]


@pytest.mark.asyncio
async def test_no_audio_reaches_inworld_before_the_caller_speaks():
    service, sock = _local_service()
    await _feed_audio(service, 50)  # one second of silence or noise
    assert sock.sent == []


@pytest.mark.asyncio
async def test_voice_opens_the_gate_with_at_most_half_a_second_of_lead_in():
    service, sock = _local_service()
    await _feed_audio(service, 50)  # 1 s before the voice is detected
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    sent_bytes = sum(len(__import__("base64").b64decode(m["audio"])) for m in sock.sent)
    buffered = len(service._audio_buffer)
    lead_in_secs = (sent_bytes + buffered) / (RATE * 2)
    assert 0.45 <= lead_in_secs <= 0.5
    await _feed_audio(service, 10)
    assert sock.types().count("input_audio_buffer.append") >= 5


@pytest.mark.asyncio
async def test_a_turn_with_speech_commits_and_asks_for_a_reply():
    service, sock = _local_service()
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    await service._track_local_turn_frame(UserStartedSpeakingFrame())
    await _feed_audio(service, 30)
    await service._track_local_turn_frame(VADUserStoppedSpeakingFrame())
    await service._handle_user_stopped_speaking(SimpleNamespace())

    types = sock.types()
    assert types[-2:] == ["input_audio_buffer.commit", "response.create"]
    assert "input_audio_buffer.clear" not in types
    assert service._audio_gate_open is False
    # After the turn, audio goes back to the lead-in buffer, not to Inworld.
    before = len(sock.sent)
    await _feed_audio(service, 20)
    assert len(sock.sent) == before


@pytest.mark.asyncio
async def test_a_turn_with_no_audio_sent_never_commits():
    service, sock = _local_service()
    service._user_is_muted = True
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    await service._track_local_turn_frame(UserStartedSpeakingFrame())
    await _feed_audio(service, 30)
    await service._handle_user_stopped_speaking(SimpleNamespace())
    assert "input_audio_buffer.commit" not in sock.types()
    assert "response.create" not in sock.types()


@pytest.mark.asyncio
async def test_voice_that_never_became_a_turn_is_thrown_away_after_real_silence():
    service, sock = _local_service()
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    await _feed_audio(service, 30)
    await service._track_local_turn_frame(VADUserStoppedSpeakingFrame())  # no turn started
    await _feed_audio(service, 20)  # a 0.4 s pause: might be between words
    assert "input_audio_buffer.clear" not in sock.types()
    await _feed_audio(service, 15)  # 0.7 s of silence in all: give up on it
    assert sock.types()[-1] == "input_audio_buffer.clear"
    assert service._audio_pending is False and service._audio_buffer == b""
    before = len(sock.sent)
    await _feed_audio(service, 20)
    assert len(sock.sent) == before


@pytest.mark.asyncio
async def test_a_real_interruption_cancels_the_reply_but_keeps_the_callers_words():
    service, sock = _local_service()
    await service._on_server_event({"type": "response.created"})
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    await _feed_audio(service, 10)
    await service._handle_interruption()
    types = sock.types()
    assert "response.cancel" in types
    assert "input_audio_buffer.clear" not in types
    assert service._audio_pending is True


@pytest.mark.asyncio
async def test_an_interruption_with_no_reply_running_sends_no_cancel():
    service, sock = _local_service()
    await service._handle_interruption()
    assert "response.cancel" not in sock.types()


@pytest.mark.asyncio
async def test_an_interruption_cancels_a_reply_asked_for_but_not_yet_started():
    service, sock = _local_service()
    await service.send_client_event(events.ResponseCreateEvent())
    await service._handle_interruption()
    assert sock.types() == ["response.create", "response.cancel"]


@pytest.mark.asyncio
async def test_an_interruption_drops_a_held_reply():
    service, sock = _local_service()
    await service._on_server_event({"type": "response.created"})
    await service.send_client_event(events.ResponseCreateEvent())  # held
    assert service._held_response_create is not None
    await service._handle_interruption()
    assert service._held_response_create is None


@pytest.mark.asyncio
async def test_muted_audio_never_reaches_inworld_even_with_the_gate_open():
    service, sock = _local_service()
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    service._user_is_muted = True
    await _feed_audio(service, 30)
    assert "input_audio_buffer.append" not in sock.types()


@pytest.mark.asyncio
async def test_a_pause_between_words_keeps_the_callers_first_words():
    service, sock = _local_service()
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    await _feed_audio(service, 25)
    await service._track_local_turn_frame(VADUserStoppedSpeakingFrame())
    await _feed_audio(service, 10)  # 0.2 s pause
    await service._track_local_turn_frame(VADUserStartedSpeakingFrame())
    await _feed_audio(service, 25)
    await service._track_local_turn_frame(UserStartedSpeakingFrame())  # interruption decided
    await service._handle_user_stopped_speaking(SimpleNamespace())
    types = sock.types()
    assert "input_audio_buffer.clear" not in types
    assert types[-2:] == ["input_audio_buffer.commit", "response.create"]
    sent = sum(len(__import__("base64").b64decode(m["audio"])) for m in sock.sent if m["type"] == "input_audio_buffer.append")
    assert sent / (RATE * 2) >= 1.1  # all 1.2 s of speech + pause reached Inworld


def test_an_empty_commit_error_cannot_kill_the_session():
    assert "input_audio_buffer_commit_empty" in inworld_llm._NON_FATAL_ERROR_CODES


@pytest.mark.asyncio
async def test_inworld_modes_other_than_local_still_stream_all_audio():
    service = DograhInworldRealtimeLLMService(api_key="k")
    service._llm_needs_conversation_setup = False
    sock = _FakeSocket()
    service._websocket = sock
    await _feed_audio(service, 10)
    assert sock.types().count("input_audio_buffer.append") >= 3


def test_factory_builds_a_manual_mode_service_for_dograh_local():
    effective_config = EffectiveAIModelConfiguration(
        is_realtime=True,
        realtime=InworldRealtimeLLMConfiguration(api_key="k", turn_detection="dograh_local"),
    )
    service = create_realtime_llm_service(effective_config, audio_config=SimpleNamespace())
    assert service._local_turns is True
    assert service._is_manual_turn_detection() is True
