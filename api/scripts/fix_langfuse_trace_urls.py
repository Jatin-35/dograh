"""Rewrite legacy Langfuse trace links on past calls to the v4 form.

Langfuse Cloud went v4-only on 2026-11-16, and its ``/trace/<id>`` shortcut no
longer resolves traces ingested after that. Calls store their link in
``gathered_context.trace_url``; this rewrites ``<host>/trace/<id>`` to
``<host>/project/<project_id>/traces/<id>``. The trace id is unchanged: the data
was always in Langfuse, only the link was wrong. The project-scoped form also
opens traces ingested before the cutover, so every legacy link of a known
project is rewritten (ported from upstream Dograh c3a53f7a, minus its page
probe, which can't see past Langfuse Cloud's login redirect).

The project id comes from the org's saved Langfuse settings (looked up from
Langfuse with its keys when not saved yet), or LANGFUSE_PROJECT_ID / the env
keys for links on the deployment's default host. Links on a host with no known
project are left alone. Safe to re-run.

Nothing is written unless --apply is given. On the deployed host:

    sudo docker compose -p dograh exec api python -m api.scripts.fix_langfuse_trace_urls
    sudo docker compose -p dograh exec api python -m api.scripts.fix_langfuse_trace_urls --apply

It lives under api/ because the Dockerfile copies ./api wholesale.
"""

import argparse
import asyncio
import re

from sqlalchemy import text

from api.constants import (
    LANGFUSE_HOST,
    LANGFUSE_PROJECT_ID,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
)
from api.db import db_client
from api.enums import OrganizationConfigurationKey
from api.services.pipecat.tracing_config import (
    fetch_langfuse_project_id,
    normalize_langfuse_host,
)

LEGACY_URL = re.compile(r"^(?P<host>https?://[^/]+)/trace/(?P<trace_id>[0-9a-fA-F]+)$")


async def org_projects() -> dict:
    """org id -> (host, project_id or None)."""
    configs = await db_client.get_all_configurations_by_key(
        OrganizationConfigurationKey.LANGFUSE_CREDENTIALS.value,
    )
    out = {}
    for config in configs:
        value = config["value"] or {}
        project_id = value.get("project_id") or await fetch_langfuse_project_id(
            value.get("host"), value.get("public_key"), value.get("secret_key")
        )
        out[config["organization_id"]] = (
            normalize_langfuse_host(value.get("host")),
            project_id,
        )
    return out


def plan_rewrites(rows, projects, default_host, default_project):
    """[(run_id, new_url)] and a count of links with no known project."""
    planned, unresolved = [], 0
    for row in rows:
        match = LEGACY_URL.match(row["trace_url"] or "")
        if not match:
            continue
        host = normalize_langfuse_host(match.group("host"))
        org_host, org_project = projects.get(row["organization_id"], (None, None))
        if org_host and host == org_host and org_project:
            project_id = org_project
        elif default_host and host == default_host and default_project:
            project_id = default_project
        else:
            unresolved += 1
            continue
        planned.append(
            (row["id"], f"{host}/project/{project_id}/traces/{match.group('trace_id')}")
        )
    return planned, unresolved


async def main(apply: bool) -> None:
    projects = await org_projects()
    default_host = normalize_langfuse_host(LANGFUSE_HOST)
    default_project = LANGFUSE_PROJECT_ID or await fetch_langfuse_project_id(
        LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
    )

    async with db_client.async_session() as session:
        result = await session.execute(
            text("""
                SELECT r.id, r.gathered_context->>'trace_url' AS trace_url,
                       w.organization_id
                FROM workflow_runs r
                JOIN workflows w ON w.id = r.workflow_id
                WHERE r.gathered_context->>'trace_url' LIKE '%/trace/%'
                  AND r.gathered_context->>'trace_url' NOT LIKE '%/project/%'
                ORDER BY r.id
            """)
        )
        rows = list(result.mappings())

    planned, unresolved = plan_rewrites(rows, projects, default_host, default_project)
    print(f"{len(rows)} call(s) hold a legacy trace link")
    print(f"  {len(planned)} to rewrite")
    if unresolved:
        print(f"  {unresolved} on a host with no known Langfuse project, left alone")
    for run_id, url in planned[:5]:
        print(f"    e.g. run={run_id} -> {url}")

    if not apply:
        print("\nDry run, nothing written. Re-run with --apply to write.")
        return

    async with db_client.async_session() as session:
        for run_id, url in planned:
            await session.execute(
                text("""
                    UPDATE workflow_runs
                    SET gathered_context = cast(
                        jsonb_set(
                            cast(gathered_context as jsonb),
                            '{trace_url}',
                            to_jsonb(cast(:u as text))
                        ) as json)
                    WHERE id = :id
                """),
                {"u": url, "id": run_id},
            )
        await session.commit()
    print(f"\nRewrote {len(planned)} trace link(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes.")
    asyncio.run(main(parser.parse_args().apply))
