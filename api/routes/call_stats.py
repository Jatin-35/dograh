from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.call_report.stats import (
    StatsRangeError,
    build_stats_response,
    resolve_range,
)

router = APIRouter(prefix="/organizations/reports")


class StatsRange(BaseModel):
    start_date: str
    end_date: str
    timezone: str


class StatsKpis(BaseModel):
    total_calls: int
    inbound_calls: int
    outbound_calls: int
    successful_calls: int
    failed_calls: int
    success_rate: Optional[float] = None
    avg_duration_seconds: Optional[float] = None
    total_duration_seconds: float
    calls_with_ticket: int
    tickets_created: int
    calls_with_closed_ticket: int
    calls_with_open_ticket: int
    # Calls QA actually analysed — the base for sentiment, satisfaction and reasons.
    analysed_calls: int


class StatsCapabilities(BaseModel):
    # False when none of the agents in scope has a ticket tool / a QA node.
    tickets: bool
    analysis: bool


class StatsItem(BaseModel):
    key: str
    label: str
    count: int


class StatsDay(BaseModel):
    date: str
    calls: int
    successful: int


class StatsHour(BaseModel):
    hour: int
    calls: int


class CallStatsResponse(BaseModel):
    range: StatsRange
    kpis: StatsKpis
    capabilities: StatsCapabilities
    outcomes: list[StatsItem]
    disconnections: list[StatsItem]
    sentiment: list[StatsItem]
    satisfaction: list[StatsItem]
    reasons: list[StatsItem]
    daily: list[StatsDay]
    hourly: list[StatsHour]


@router.get("/call-stats")
async def get_call_stats(
    start_date: date = Query(..., description="First day, YYYY-MM-DD (local)"),
    end_date: date = Query(..., description="Last day inclusive, YYYY-MM-DD (local)"),
    timezone: str = Query(..., description="IANA timezone, e.g. 'Asia/Kolkata'"),
    workflow_id: Optional[int] = Query(None, description="Limit to one agent"),
    call_type: Optional[Literal["inbound", "outbound"]] = Query(None),
    include_test_calls: bool = Query(
        False,
        description="Include browser/test sessions, which are left out by default",
    ),
    user: UserModel = Depends(get_user),
) -> CallStatsResponse:
    """KPIs and chart series for the organization's calls over a date range."""
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    try:
        start_utc, end_utc, _ = resolve_range(start_date, end_date, timezone)
    except StatsRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw = await db_client.get_call_stats(
        organization_id=user.selected_organization_id,
        start_utc=start_utc,
        end_utc=end_utc,
        timezone=timezone,
        workflow_id=workflow_id,
        call_type=call_type,
        include_test_calls=include_test_calls,
    )
    return CallStatsResponse(
        **build_stats_response(
            raw, start_date=start_date, end_date=end_date, timezone=timezone
        )
    )
