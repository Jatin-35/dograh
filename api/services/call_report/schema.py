"""The per-call report: one normalized, agent-independent record of a call.

Everything a dashboard or run page needs about a call, built once after the call
ends and stored (see ``api/db/call_report_client.py``). ``schema_version`` lets a
stored report be recognised — and rebuilt — if the shape ever changes.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class TicketInfo(BaseModel):
    ticket_id: str
    created_at: Optional[str] = None  # ISO-8601 UTC, from the tool-result event
    closed: bool = False
    closed_at: Optional[str] = None
    source: str  # which ticket integration produced it, e.g. "think_gas_sap"


class NodeAnalysis(BaseModel):
    """One QA node-segment's view of the call, after normalisation."""

    node_id: str
    node_name: str = ""
    sentiment: Optional[str] = None
    satisfied: Optional[bool] = None
    mood: Optional[str] = None
    reason_for_call: Optional[str] = None
    resolved: Optional[bool] = None
    human_transfer: Optional[bool] = None
    summary: Optional[str] = None
    quality_score: Optional[float] = None
    tags: list[str] = Field(default_factory=list)


class CallSection(BaseModel):
    call_type: Optional[str] = None  # inbound | outbound
    mode: str = ""
    is_telephony: bool = True
    started_at: str
    duration_seconds: Optional[float] = None
    phone_number: Optional[str] = None


class DisconnectSection(BaseModel):
    reason: Optional[str] = None  # raw stored value, e.g. "user_hangup"
    category: str
    label: str


class OutcomeSection(BaseModel):
    code: str
    label: str
    successful: bool


class TicketSection(BaseModel):
    created: bool = False
    closed: bool = False
    count: int = 0
    tickets: list[TicketInfo] = Field(default_factory=list)


class AnalysisSection(BaseModel):
    # analysed | skipped | error | not_run
    status: str = "not_run"
    skipped_reason: Optional[str] = None
    sentiment: Optional[str] = None
    satisfied: Optional[bool] = None
    mood: Optional[str] = None
    reason_for_call: Optional[str] = None
    resolved: Optional[bool] = None
    human_transfer: Optional[bool] = None
    summary: Optional[str] = None
    quality_score: Optional[float] = None
    tags: list[str] = Field(default_factory=list)
    nodes: list[NodeAnalysis] = Field(default_factory=list)


class CapabilitiesSection(BaseModel):
    """What this call's agent is set up to produce — decides which sections apply.

    A section that the agent cannot feed (no ticket tool, no QA node) is not shown:
    an empty "Ticket: none created" implies tickets are part of the agent's job.
    """

    ticket: bool = False
    analysis: bool = False


class CallReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: int
    workflow_id: int
    organization_id: int
    call: CallSection
    disconnect: DisconnectSection
    outcome: OutcomeSection
    ticket: TicketSection
    analysis: AnalysisSection
    capabilities: CapabilitiesSection = Field(default_factory=CapabilitiesSection)
    # Values the agent captured during the call (its extracted variables), as
    # the agent named them — e.g. interested_item, product_category.
    captured: dict[str, Any] = Field(default_factory=dict)
