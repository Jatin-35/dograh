"""Request/response schemas for Webhook Sync (CRM webhook → lead → call)."""

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AuthType = Literal["api_key", "hmac", "url_token"]
RetryStatus = Literal["no_answer", "busy", "failed"]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class FieldMapping(BaseModel):
    """Where each standard field sits in the CRM payload (dotted paths,
    e.g. ``data.lead.mobile``). Unmapped fields are auto-detected."""

    model_config = ConfigDict(extra="forbid")

    phone: Optional[str] = Field(default=None, max_length=255)
    name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    external_lead_id: Optional[str] = Field(default=None, max_length=255)
    source: Optional[str] = Field(default=None, max_length=255)
    city: Optional[str] = Field(default=None, max_length=255)
    language_preference: Optional[str] = Field(default=None, max_length=255)
    # Extra call variables: {"product": "data.product"} → {{product}}.
    custom: Dict[str, str] = Field(default_factory=dict)

    @field_validator("custom")
    @classmethod
    def _limit_custom(cls, value: Dict[str, str]) -> Dict[str, str]:
        if len(value) > 50:
            raise ValueError("At most 50 custom fields")
        for key, path in value.items():
            if not key.strip() or len(key) > 64 or len(path) > 255:
                raise ValueError("Custom field names are 1-64 chars, paths up to 255")
        return value


class CallingHours(BaseModel):
    """When calls may be placed. Default 09:00-21:00 IST every day (TRAI)."""

    start: str = "09:00"
    end: str = "21:00"
    timezone: str = "Asia/Kolkata"
    # 0 = Monday ... 6 = Sunday
    days: List[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("Use 24-hour HH:MM")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError(f"Unknown timezone: {value}")
        return value

    @field_validator("days")
    @classmethod
    def _days(cls, value: List[int]) -> List[int]:
        if not value or any(d < 0 or d > 6 for d in value):
            raise ValueError("Days are 0 (Monday) to 6 (Sunday), at least one")
        return sorted(set(value))

    @model_validator(mode="after")
    def _window(self) -> "CallingHours":
        if self.start >= self.end:
            raise ValueError("Calling hours must end after they start")
        return self


class RetrySettings(BaseModel):
    # Total call attempts per lead, including the first.
    max_attempts: int = Field(default=3, ge=1, le=10)
    gap_minutes: int = Field(default=30, ge=5, le=1440)
    on_statuses: List[RetryStatus] = Field(
        default_factory=lambda: ["no_answer", "busy"]
    )


class CallSettings(BaseModel):
    delay_minutes: int = Field(default=0, ge=0, le=1440)
    calling_hours: CallingHours = Field(default_factory=CallingHours)
    retries: RetrySettings = Field(default_factory=RetrySettings)
    # Same phone on the same endpoint within this window → duplicate, no call.
    dedupe_window_hours: int = Field(default=24, ge=0, le=720)
    # Where to POST the call result back (Phase 4). HTTPS only.
    callback_url: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("callback_url")
    @classmethod
    def _https(cls, value: Optional[str]) -> Optional[str]:
        if value and not value.lower().startswith("https://"):
            raise ValueError("The callback URL must use https://")
        return value or None


class WebhookEndpointCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    workflow_id: int
    auth_type: AuthType = "api_key"
    is_active: bool = True
    auto_call: bool = True
    field_mapping: FieldMapping = Field(default_factory=FieldMapping)
    call_settings: CallSettings = Field(default_factory=CallSettings)
    rate_limit_per_minute: int = Field(default=100, ge=1, le=1000)


class WebhookEndpointUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    workflow_id: Optional[int] = None
    auth_type: Optional[AuthType] = None
    is_active: Optional[bool] = None
    auto_call: Optional[bool] = None
    field_mapping: Optional[FieldMapping] = None
    call_settings: Optional[CallSettings] = None
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1, le=1000)


class WebhookEndpointResponse(BaseModel):
    id: int
    name: str
    endpoint_uuid: str
    webhook_url: str
    auth_type: AuthType
    secret: str
    workflow_id: int
    workflow_name: Optional[str] = None
    campaign_id: Optional[int] = None
    is_active: bool
    auto_call: bool
    field_mapping: FieldMapping
    call_settings: CallSettings
    rate_limit_per_minute: int
    leads_today: int = 0
    leads_total: int = 0
    created_at: datetime
    updated_at: datetime


class WebhookEndpointListResponse(BaseModel):
    endpoints: List[WebhookEndpointResponse]


class WebhookLeadResponse(BaseModel):
    id: int
    endpoint_id: int
    external_lead_id: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    phone_raw: Optional[str] = None
    email: Optional[str] = None
    source: Optional[str] = None
    variables: Dict[str, Any] = Field(default_factory=dict)
    status: str
    status_reason: Optional[str] = None
    duplicate_of_lead_id: Optional[int] = None
    call_attempts: int = 0
    last_call_status: Optional[str] = None
    disposition: Optional[str] = None
    last_workflow_run_id: Optional[int] = None
    next_retry_at: Optional[datetime] = None
    received_at: datetime
    updated_at: datetime
    # Only on the single-lead read, and hidden when numbers are masked.
    raw_payload: Optional[Any] = None


class WebhookLeadListResponse(BaseModel):
    leads: List[WebhookLeadResponse]
    total: int


class WebhookRequestLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    endpoint_id: int
    method: str
    content_type: Optional[str] = None
    headers: Dict[str, Any] = Field(default_factory=dict)
    raw_body: Optional[str] = None
    body_truncated: bool = False
    response_code: int
    error: Optional[str] = None
    ip: Optional[str] = None
    lead_ids: Optional[List[int]] = None
    received_at: datetime


class WebhookRequestLogListResponse(BaseModel):
    logs: List[WebhookRequestLogResponse]
    total: int


class WebhookAuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    endpoint_id: Optional[int] = None
    endpoint_name: Optional[str] = None
    user_id: Optional[int] = None
    # created, updated, paused, resumed, secret_regenerated, deleted
    action: str
    changes: Optional[Dict[str, Any]] = None
    created_at: datetime


class WebhookAuditLogListResponse(BaseModel):
    entries: List[WebhookAuditLogResponse]
    total: int
