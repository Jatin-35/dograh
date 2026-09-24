"""Request shape for POST /lookup.

The response is a plain dict keyed by ``status`` (see matching.py) — the bot
branches on ``status`` and reads ``message`` for what to say next, so a rigid
response model would only add a second place to keep in sync.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    product: str = Field(min_length=1, max_length=50,
                         description='"Water tank" or "Moundling" (common variants accepted)')
    city: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)

    @field_validator("city", "district", "state", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        # LLM tool calls often send "" or "null" for fields they don't have.
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "na", "n/a"}:
            return None
        return v
