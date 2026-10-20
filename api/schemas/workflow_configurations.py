from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_MAX_CALL_DURATION_SECONDS = 300
# Hard ceiling on configurable call duration. Must stay <= the concurrency
# rate limiter's stale_call_timeout (20 min): a call running past that has
# its slot purged as stale and the org concurrency limit under-counts.
MAX_CALL_DURATION_SECONDS = 1200
DEFAULT_MAX_USER_IDLE_TIMEOUT_SECONDS = 10.0
DEFAULT_SMART_TURN_STOP_SECS = 2.0
# Two transcribed words to interrupt the bot, so "haan" / "mhm" / a cough
# don't (upstream dograh #832).
DEFAULT_TURN_START_STRATEGY = "min_words"
DEFAULT_TURN_START_MIN_WORDS = 2
DEFAULT_TURN_STOP_STRATEGY = "transcription"
DEFAULT_CONTEXT_COMPACTION_ENABLED = False
DEFAULT_NOISE_REDUCTION = "off"


class AmbientNoiseConfigurationDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    # 0.1-1.5: above 1.0 boosts a soft sound for the short-wait play modes.
    volume: float = Field(default=0.3, ge=0.1, le=1.5)
    # When the sound plays: the whole call, only while the bot is working out
    # its reply (after the caller's turn, including tool calls), or only while
    # a tool runs. See api/services/pipecat/ambient_sound.py.
    play_mode: Literal["whole_call", "thinking", "tool_calls"] = "whole_call"
    # Wait this long into a pause before playing, so quick replies stay silent.
    start_delay_secs: float = Field(default=0.5, ge=0, le=5)
    # Built-in sound; an uploaded file (storage_key) takes precedence.
    sound: Literal["office", "typing"] = "office"

    @field_validator("volume", mode="before")
    @classmethod
    def _clamp_volume(cls, value):
        # Agents saved before the range existed can hold 0 or 3; re-saving
        # them must not fail, so a stored number is brought into range.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return min(1.5, max(0.1, float(value)))
        return value


class WorkflowConfigurationDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _treat_null_as_unset(cls, data):
        # Stored configs (and older clients) carry explicit JSON nulls for
        # keys the user never configured; dropping them lets the field
        # defaults apply instead of failing validation.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data

    ambient_noise_configuration: AmbientNoiseConfigurationDefaults = Field(
        default_factory=AmbientNoiseConfigurationDefaults
    )
    max_call_duration: int = Field(
        default=DEFAULT_MAX_CALL_DURATION_SECONDS,
        gt=0,
        le=MAX_CALL_DURATION_SECONDS,
    )
    max_user_idle_timeout: float = DEFAULT_MAX_USER_IDLE_TIMEOUT_SECONDS
    smart_turn_stop_secs: float = DEFAULT_SMART_TURN_STOP_SECS
    turn_start_strategy: Literal["default", "min_words"] = DEFAULT_TURN_START_STRATEGY
    turn_start_min_words: int = Field(default=DEFAULT_TURN_START_MIN_WORDS, ge=1)
    turn_stop_strategy: Literal["transcription", "turn_analyzer"] = (
        DEFAULT_TURN_STOP_STRATEGY
    )
    dictionary: str = ""
    context_compaction_enabled: bool = DEFAULT_CONTEXT_COMPACTION_ENABLED
    # Caller-side noise suppression applied in the input transport, before
    # STT / the realtime model hears the audio.
    noise_reduction: Literal["off", "rnnoise"] = DEFAULT_NOISE_REDUCTION

    @field_validator("turn_start_strategy", mode="before")
    @classmethod
    def _retire_provisional_vad(cls, value: object) -> object:
        # "provisional_vad" was retired (upstream dograh #767): with external-turn
        # STTs it could leave a turn open and the bot silent. Saved agents that
        # still carry it get the two-word default instead of failing to load.
        return DEFAULT_TURN_START_STRATEGY if value == "provisional_vad" else value


def get_default_workflow_configurations() -> WorkflowConfigurationDefaults:
    return WorkflowConfigurationDefaults()
