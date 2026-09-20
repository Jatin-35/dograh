from fastapi import APIRouter, Depends, HTTPException

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.call_report.builder import build_call_report
from api.services.call_report.schema import CallReport
from api.services.call_report.service import snapshot_for_run
from api.services.phone_masking import mask_phone_number, should_mask_phone_numbers

router = APIRouter(prefix="/workflow")


class CallReportResponse(CallReport):
    # True when the customer number in this response has been masked, so the UI
    # can also withhold the raw context blocks that would show it in full.
    phone_masked: bool = False


def _respond(report: CallReport, masked: bool) -> CallReportResponse:
    data = report.model_dump()
    if masked:
        data["call"]["phone_number"] = mask_phone_number(data["call"]["phone_number"])
    return CallReportResponse(**data, phone_masked=masked)


@router.get("/{workflow_id}/runs/{run_id}/call-report")
async def get_run_call_report(
    workflow_id: int, run_id: int, user: UserModel = Depends(get_user)
) -> CallReportResponse:
    """The normalized report for one call.

    Served from the stored row when there is one. A run that finished before
    reports existed has none, so one is built on the fly from what the run holds —
    read-only, nothing is written on a GET.
    """
    organization_id = user.selected_organization_id

    run = await db_client.get_workflow_run(run_id, organization_id=organization_id)
    if not run or run.workflow_id != workflow_id:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    masked = await should_mask_phone_numbers(user)

    stored = await db_client.get_call_report(run_id, organization_id)
    if stored:
        return _respond(stored, masked)

    full_run, run_organization_id = await db_client.get_workflow_run_with_context(
        run_id
    )
    if not full_run or run_organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    return _respond(
        build_call_report(await snapshot_for_run(full_run, run_organization_id)), masked
    )
