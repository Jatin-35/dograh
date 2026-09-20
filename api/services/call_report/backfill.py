"""Build reports for calls that finished before reports existed.

Reports are written as calls complete, so a deployment that already has history
starts with an empty dashboard. This fills it in from what each run already
stores. It makes no model calls and costs nothing: ticket facts come from the
tool results in the call log, and analysis fields from whatever QA output the run
already holds (a run QA never analysed simply has none).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from loguru import logger

from api.db import db_client
from api.services.call_report.service import build_and_store_call_report

DEFAULT_BATCH_SIZE = 200


@dataclass
class BackfillSummary:
    # Runs found needing a report.
    scanned: int = 0
    # Reports written (always 0 on a dry run).
    built: int = 0
    # Runs that vanished or belong to no organization between listing and building.
    skipped: int = 0
    failed: int = 0
    failed_run_ids: list[int] = field(default_factory=list)


async def backfill_call_reports(
    *,
    apply: bool = False,
    organization_id: Optional[int] = None,
    workflow_id: Optional[int] = None,
    since: Optional[datetime] = None,
    limit: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    rebuild_existing: bool = False,
    on_batch: Optional[Callable[[BackfillSummary], None]] = None,
) -> BackfillSummary:
    """Build reports for completed runs, in batches, without touching anything
    unless ``apply`` is set.

    Safe to run repeatedly and to interrupt: a report is a pure function of its
    run, existing reports are left alone unless ``rebuild_existing``, and one run
    that fails to build is recorded and skipped rather than stopping the rest.
    """
    summary = BackfillSummary()
    after_id = 0

    while limit is None or summary.scanned < limit:
        remaining = None if limit is None else limit - summary.scanned
        page = batch_size if remaining is None else min(batch_size, remaining)

        run_ids = await db_client.list_run_ids_for_report_backfill(
            after_id=after_id,
            limit=page,
            organization_id=organization_id,
            workflow_id=workflow_id,
            since=since,
            only_missing=not rebuild_existing,
        )
        if not run_ids:
            break

        for run_id in run_ids:
            after_id = run_id
            summary.scanned += 1
            if not apply:
                continue
            try:
                report = await build_and_store_call_report(run_id)
            except Exception as exc:
                summary.failed += 1
                if len(summary.failed_run_ids) < 50:
                    summary.failed_run_ids.append(run_id)
                logger.warning(f"Could not build a report for run {run_id}: {exc}")
                continue
            if report is None:
                summary.skipped += 1
            else:
                summary.built += 1

        if on_batch:
            on_batch(summary)

    return summary
