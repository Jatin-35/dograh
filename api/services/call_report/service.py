"""Build and store a call's report. Called once the call's post-processing is done."""

from typing import Optional

from loguru import logger

from api.db import db_client
from api.services.call_report.builder import RunSnapshot, build_call_report
from api.services.call_report.capabilities import (
    qa_enabled_in_definition,
    tool_function_name,
    tool_uuids_in_definition,
)
from api.services.call_report.schema import CallReport


async def snapshot_for_run(run, organization_id: int) -> RunSnapshot:
    """A run's snapshot, with what its agent is set up to do read from the definition
    the run was pinned to (the version that actually ran, not the current draft)."""
    definition = run.definition.workflow_json if run.definition else None

    tool_names: frozenset = frozenset()
    tool_uuids = tool_uuids_in_definition(definition)
    if tool_uuids:
        try:
            tools = await db_client.get_tools_by_uuids(
                list(tool_uuids), organization_id
            )
            tool_names = frozenset(tool_function_name(tool.name) for tool in tools)
        except Exception as exc:
            # Capabilities are a display hint; without them the report falls back to
            # inferring from the call's own data rather than failing.
            logger.warning(f"Could not read the agent's tools for run {run.id}: {exc}")

    return RunSnapshot.from_run(
        run,
        organization_id,
        agent_tool_names=tool_names,
        qa_enabled=qa_enabled_in_definition(definition),
    )


async def build_and_store_call_report(workflow_run_id: int) -> Optional[CallReport]:
    """Build the report for a run from its stored data and upsert it.

    Safe to call repeatedly: the report is a pure function of the run, so a later
    call (after QA finishes, say) simply replaces the earlier one.
    """
    run, organization_id = await db_client.get_workflow_run_with_context(
        workflow_run_id
    )
    if not run or organization_id is None:
        return None

    report = build_call_report(await snapshot_for_run(run, organization_id))
    await db_client.upsert_call_report(report)
    return report


async def safe_build_and_store_call_report(workflow_run_id: int) -> None:
    """Same, but never raises: reporting must not disturb call completion."""
    try:
        await build_and_store_call_report(workflow_run_id)
    except Exception as exc:
        logger.error(
            f"Failed to build call report for run {workflow_run_id}: {exc}",
            exc_info=True,
        )
