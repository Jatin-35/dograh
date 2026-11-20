from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from api.constants import (
    MAX_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
    MIN_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
    TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
)

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
MAX_CALL_DISPOSITIONS = 50
MAX_CALL_DISPOSITION_CODE_LENGTH = 64
MAX_CALL_DISPOSITION_DESCRIPTION_LENGTH = 1_000
MAX_CALL_DISPOSITION_DESCRIPTIONS_TOTAL_LENGTH = 4_000


class CallDispositionOption(BaseModel):
    """One business outcome the terminal classifier may select."""

    code: str = Field(
        min_length=1,
        max_length=MAX_CALL_DISPOSITION_CODE_LENGTH,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
        description="Stable code recorded when this outcome is selected.",
    )
    description: str = Field(
        min_length=1,
        max_length=MAX_CALL_DISPOSITION_DESCRIPTION_LENGTH,
        description="Business criteria for selecting this disposition.",
    )

    @field_validator("code", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


DEFAULT_CALL_DISPOSITION_OPTIONS: tuple[CallDispositionOption, ...] = (
    CallDispositionOption(
        code="qualified",
        description="The call achieved the workflow's primary goal.",
    ),
    CallDispositionOption(
        code="not_interested",
        description="The person clearly declined the offer or said they are not interested.",
    ),
    CallDispositionOption(
        code="wrong_number",
        description="The call reached the wrong person or an incorrect phone number.",
    ),
    CallDispositionOption(
        code="voicemail_detected",
        description="The call reached voicemail or an answering machine instead of a person.",
    ),
    CallDispositionOption(
        code="do_not_call",
        description="The person explicitly asked not to be contacted again.",
    ),
    CallDispositionOption(
        code="callback_requested",
        description="The person asked to be contacted again at a later time.",
    ),
)


def get_default_call_disposition_options() -> list[CallDispositionOption]:
    """Return fresh copies of the built-in terminal outcome catalog."""
    return [option.model_copy(deep=True) for option in DEFAULT_CALL_DISPOSITION_OPTIONS]


class ExternalPBXFieldMapping(BaseModel):
    """Map one gathered-context value to a provider-native field."""

    context_path: str = Field(min_length=1, max_length=255)
    destination_field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

    @field_validator("context_path", mode="before")
    @classmethod
    def strip_context_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("destination_field", mode="before")
    @classmethod
    def strip_destination_field(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


# Extra lead fields to capture from the inbound INVITE, named without the
# provider's header prefix (``first_name`` -> ``X-VICIDIAL-first_name``). Each
# entry costs one ARI round trip during call setup, so the set is configured
# explicitly per workflow rather than enumerated off the INVITE.
MAX_EXTERNAL_PBX_LEAD_HEADERS = 50

ExternalPBXLeadHeader = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
]


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

    tts_cache_enabled: bool = Field(
        default=False,
        description="Reuse generated speech for repeated phrases. Supports MiniMax TTS.",
    )
    call_dispositions: list[CallDispositionOption] = Field(
        default_factory=list,
        max_length=MAX_CALL_DISPOSITIONS,
        description=(
            "Allowed business outcomes for terminal call classification. Each "
            "entry defines the exact stored code and the criteria for selecting it."
        ),
    )
    text_chat_inactivity_timeout_seconds: int = Field(
        default=TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
        ge=MIN_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
        le=MAX_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS,
    )
    external_pbx_field_mappings: list[ExternalPBXFieldMapping] = Field(
        default_factory=list,
        max_length=100,
    )
    external_pbx_lead_headers: list[ExternalPBXLeadHeader] = Field(
        default_factory=list,
        max_length=MAX_EXTERNAL_PBX_LEAD_HEADERS,
    )

    @field_validator("turn_start_strategy", mode="before")
    @classmethod
    def _coerce_retired_turn_start_strategy(cls, value: object) -> object:
        # "provisional_vad" was retired. The runtime already reads this key off
        # the raw dict and falls through to the default for anything it does not
        # recognise, so a row the data migration missed still runs correctly —
        # this keeps such a row loadable (and re-savable) through the API too.
        if value == "provisional_vad":
            return DEFAULT_TURN_START_STRATEGY
        return value

    @field_validator("call_dispositions")
    @classmethod
    def validate_call_dispositions(
        cls, value: list[CallDispositionOption]
    ) -> list[CallDispositionOption]:
        seen: set[str] = set()
        for option in value:
            normalized_code = option.code.casefold()
            if normalized_code in seen:
                raise ValueError("call disposition codes must be unique")
            seen.add(normalized_code)

        total_description_length = sum(len(option.description) for option in value)
        if total_description_length > MAX_CALL_DISPOSITION_DESCRIPTIONS_TOTAL_LENGTH:
            raise ValueError(
                "call disposition descriptions must total at most "
                f"{MAX_CALL_DISPOSITION_DESCRIPTIONS_TOTAL_LENGTH} characters"
            )
        return value

    @field_validator("external_pbx_lead_headers", mode="before")
    @classmethod
    def strip_lead_headers(cls, value: object) -> object:
        """Trim and de-duplicate while preserving the configured order."""
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            name = item.strip() if isinstance(item, str) else item
            if name and name not in cleaned:
                cleaned.append(name)
        return cleaned

    @field_validator("turn_start_strategy", mode="before")
    @classmethod
    def _retire_provisional_vad(cls, value: object) -> object:
        # "provisional_vad" was retired (upstream dograh #767): with external-turn
        # STTs it could leave a turn open and the bot silent. Saved agents that
        # still carry it get the two-word default instead of failing to load.
        return DEFAULT_TURN_START_STRATEGY if value == "provisional_vad" else value


class TextChatInactivityTimeoutConstraints(BaseModel):
    """Backend-owned timeout metadata consumed by generated API clients."""

    default_seconds: int = TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS
    minimum_seconds: int = MIN_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS
    maximum_seconds: int = MAX_TEXT_CHAT_INACTIVITY_TIMEOUT_SECONDS


def get_default_workflow_configurations() -> WorkflowConfigurationDefaults:
    return WorkflowConfigurationDefaults()
