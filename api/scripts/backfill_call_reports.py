"""Fill in call reports for calls that finished before reports existed.

The client dashboard and the run page read one stored report per call, written as
each call completes. A deployment that already has call history therefore starts
with an empty dashboard until this is run. It builds each report from what the run
already stores (no model calls, no cost) and is safe to run repeatedly or to
interrupt: it only touches runs that have no report yet.

Nothing is written unless --apply is given, so the first run just counts.

On a deployed host, run it inside the api container (the database is only
reachable from there):

    sudo docker compose exec api python -m api.scripts.backfill_call_reports
    sudo docker compose exec api python -m api.scripts.backfill_call_reports --apply

Limit it to one client, or to recent calls:

    ... --organization-id 5 --since 2026-09-01 --apply

Locally, source the backend env first per AGENTS.md so this targets the dev
database:

    set -a && source api/.env && set +a
    python -m api.scripts.backfill_call_reports --apply

It lives under api/ rather than the repo-root scripts/ because the Dockerfile
copies ./api wholesale but only whitelists individual shell entrypoints out of
./scripts, so a script placed there is absent from the image.
"""

import argparse
import asyncio
from datetime import UTC, datetime

from api.services.call_report.backfill import (
    DEFAULT_BATCH_SIZE,
    BackfillSummary,
    backfill_call_reports,
)


def _parse_since(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


async def main(args: argparse.Namespace) -> None:
    def progress(summary: BackfillSummary) -> None:
        print(
            f"  ...{summary.scanned} runs checked, {summary.built} reports built",
            flush=True,
        )

    summary = await backfill_call_reports(
        apply=args.apply,
        organization_id=args.organization_id,
        workflow_id=args.workflow_id,
        since=args.since,
        limit=args.limit,
        batch_size=args.batch_size,
        rebuild_existing=args.rebuild_existing,
        on_batch=progress if args.apply else None,
    )

    if not args.apply:
        print(
            f"{summary.scanned} completed run(s) need a report. "
            "Nothing was written. Re-run with --apply to build them."
        )
        return

    print(
        f"\nDone. {summary.scanned} run(s) checked: {summary.built} report(s) built, "
        f"{summary.skipped} skipped, {summary.failed} failed."
    )
    if summary.failed_run_ids:
        print(f"Failed run ids: {summary.failed_run_ids}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Build and store the reports. Without this, nothing is written.",
    )
    parser.add_argument("--organization-id", type=int, help="Only this organization.")
    parser.add_argument("--workflow-id", type=int, help="Only this agent.")
    parser.add_argument(
        "--since",
        type=_parse_since,
        help="Only runs created on or after this date (YYYY-MM-DD, UTC).",
    )
    parser.add_argument("--limit", type=int, help="Stop after this many runs.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--rebuild-existing",
        action="store_true",
        help="Also rebuild runs that already have a report (for instance after "
        "the report logic changed).",
    )
    asyncio.run(main(parser.parse_args()))
