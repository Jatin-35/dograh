"""Request shapes for POST /lookup, POST /tanks and POST /products.

The response is a plain dict keyed by ``status`` (see matching.py) — the bot
branches on ``status`` and reads ``message`` for what to say next, so a rigid
response model would only add a second place to keep in sync.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
        return _blank_to_none(v)


def _blank_to_none(v):
    # LLM tool calls often send "" or "null" for fields they don't have.
    if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "na", "n/a"}:
        return None
    return v


Loose = str | int | float | None  # the bot may send 1100, 1100.0 or "1100 litre"


class TankRequest(BaseModel):
    """POST /tanks. Every field is optional; send only what the customer said."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # Text fields also take a number ("model": 90 → "90"); anything else
    # (a list, an object) is a 422.
    brand: Loose = Field(default=None)
    state: Loose = Field(default=None)
    model: Loose = Field(default=None)
    capacity: Loose = Field(default=None)
    colour: Loose = Field(default=None)
    layers: Loose = Field(default=None)
    guarantee: Loose = Field(default=None)
    mould: Loose = Field(default=None)
    need: Loose = Field(default=None)
    # "full" adds layer names and features to a single-tank answer; "summary"
    # gives one line. Comparisons always come in full.
    detail: Loose = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _american_spelling(cls, data):
        if isinstance(data, dict) and "color" in data and not data.get("colour"):
            data = {**data, "colour": data["color"]}
        return data

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        if isinstance(v, str) and len(v) > 200:
            return v[:200]
        return _blank_to_none(v)


class ProductRequest(BaseModel):
    """POST /products. Every field is optional; send what the customer said."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    product: Loose = Field(default=None)   # "seat cover", "angle cock", "balti"
    model: Loose = Field(default=None)     # "Hush", "Chic Dual", "Ganga"
    item: Loose = Field(default=None)      # a tap item: "angle cock", "swan neck"
    category: Loose = Field(default=None)  # "bathware", "household", "garden"
    # "full" adds the product's features; otherwise left out, as on /tanks.
    detail: Loose = Field(default=None)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        if isinstance(v, str) and len(v) > 200:
            return v[:200]
        return _blank_to_none(v)
