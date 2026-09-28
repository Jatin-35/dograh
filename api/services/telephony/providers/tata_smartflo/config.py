"""Tata SmartFlo telephony configuration schemas."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

DEFAULT_TATA_SMARTFLO_API_BASE = "https://api-smartflo.tatateleservices.com"


class TataSmartfloConfigurationRequest(BaseModel):
    """Request schema for SmartFlo configuration.

    SmartFlo offers two ways to get a bearer token. Either a successful
    ``POST /v1/auth/login`` (email + password) returns an ``access_token``
    that expires in about an hour and must be refreshed continuously, or the
    account holder generates a token themselves from their SmartFlo dashboard
    (API Connect -> API Tokens), valid for up to 90 days and independently
    revocable without touching the account password. ``api_token`` stores the
    latter; when set, the provider uses it directly and never calls
    /v1/auth/login at all. Exactly one of password/api_token must be given —
    email is required either way, since inbound webhook routing matches a
    stored config by login id regardless of which auth path it uses.

    ``api_key`` is a separate thing entirely — the Click-to-Call Support key,
    which travels in the request body rather than the Authorization header, and
    which is what binds outbound calls to a configured voice bot. Outbound needs
    both; inbound needs only the login credentials, and then only to hang up.
    """

    provider: Literal["tata_smartflo"] = Field(default="tata_smartflo")
    api_base: str = Field(
        default=DEFAULT_TATA_SMARTFLO_API_BASE,
        description="SmartFlo API base URL",
    )
    email: str = Field(
        ...,
        description=(
            "SmartFlo login id. Always required — inbound webhook routing "
            "matches a stored config by this, even when api_token is used "
            "instead of password."
        ),
    )
    password: Optional[str] = Field(
        default=None,
        description=(
            "SmartFlo account password, exchanged for an hourly bearer token "
            "via /v1/auth/login. Required only if api_token is not set."
        ),
    )
    api_token: Optional[str] = Field(
        default=None,
        description=(
            "A token generated from the SmartFlo dashboard (API Connect -> "
            "API Tokens), valid up to 90 days. Used directly as the bearer "
            "token, bypassing /v1/auth/login entirely. Preferred over "
            "password: it can be revoked independently of the account login, "
            "without changing the account password."
        ),
    )
    api_key: Optional[str] = Field(
        default=None,
        description=(
            "Click-to-Call Support API key. Required for outbound only — it "
            "selects which voice bot SmartFlo streams the call to. Inbound "
            "calls never use it."
        ),
    )
    caller_id: Optional[str] = Field(
        default=None,
        description=(
            "Default caller id for outbound calls. Must be a DID registered to "
            "this account; SmartFlo rejects anything else."
        ),
    )
    from_numbers: List[str] = Field(
        default_factory=list,
        description="DIDs on this account, used to route inbound calls.",
    )
    connect_secret: Optional[str] = Field(
        default=None,
        description=(
            "Shared secret expected on inbound voice-streaming and webhook "
            "requests. SmartFlo documents no request signing, so without this "
            "the connect endpoint is open: anyone who learns the URL can make "
            "us create runs and consume concurrency slots."
        ),
    )

    @model_validator(mode="after")
    def _require_api_key_with_caller_id(self) -> "TataSmartfloConfigurationRequest":
        # caller_id alone cannot place a call — the Click-to-Call key is what
        # actually carries the request. Catching it here beats a 400 from
        # SmartFlo on the first outbound attempt.
        if self.caller_id and not self.api_key:
            raise ValueError(
                "SmartFlo caller_id is only used for outbound calls, which also "
                "require api_key (the Click-to-Call Support key)."
            )
        return self

    @model_validator(mode="after")
    def _require_password_or_api_token(self) -> "TataSmartfloConfigurationRequest":
        # Neither auth path is optional to have entirely — without one of
        # them there is no way to mint or hold a bearer token at all.
        if not self.password and not self.api_token:
            raise ValueError(
                "SmartFlo requires either password or api_token to authenticate."
            )
        return self


class TataSmartfloConfigurationResponse(BaseModel):
    """Response schema for SmartFlo configuration with masked sensitive fields."""

    provider: Literal["tata_smartflo"] = Field(default="tata_smartflo")
    api_base: str = DEFAULT_TATA_SMARTFLO_API_BASE
    email: Optional[str] = None  # Masked
    password: Optional[str] = None  # Masked
    api_token: Optional[str] = None  # Masked
    api_key: Optional[str] = None  # Masked
    connect_secret: Optional[str] = None  # Masked
    caller_id: Optional[str] = None
    from_numbers: List[str] = Field(default_factory=list)
