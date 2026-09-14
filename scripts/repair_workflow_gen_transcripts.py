"""Repair Scout chat transcripts that are too large to send to the model.

A tool that returned a customer's raw HTTP response could write megabytes into
one message. Because the transcript is persisted and replayed on every turn,
such a thread is not merely degraded — it is dead, failing with the same 400
forever.

`agent_loop` now repairs a transcript on load, so an affected thread heals
itself the moment someone sends another message. This script exists to do it
eagerly across every session, so nobody has to hit the error first, and to
report what was there.

    # See what would change; writes nothing.
    python -m scripts.repair_workflow_gen_transcripts

    # Apply it.
    python -m scripts.repair_workflow_gen_transcripts --apply

Per AGENTS.md, source the backend env first so this targets the right database:

    set -a && source api/.env && set +a
"""

import argparse
import asyncio
import json

from sqlalchemy.future import select

from api.db import db_client
from api.db.models import WorkflowGenChatSessionModel
from api.services.workflow_gen.transcript import (
    MAX_TOOL_RESULT_CHARS,
    sanitize_transcript,
)


def _transcript_chars(messages: list) -> int:
    return len(json.dumps(messages, default=str))


def _largest_message(messages: list) -> tuple[int, str]:
    largest, role = 0, "-"
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and len(content) > largest:
            largest, role = len(content), str(message.get("role"))
    return largest, role


async def main(apply: bool) -> None:
    async with db_client.async_session() as session:
        result = await session.execute(
            select(WorkflowGenChatSessionModel).order_by(WorkflowGenChatSessionModel.id)
        )
        sessions = list(result.scalars().all())

    print(f"Scanning {len(sessions)} chat session(s)…\n")

    affected = 0
    for chat_session in sessions:
        messages = list(chat_session.messages or [])
        if not messages:
            continue

        before_chars = _transcript_chars(messages)
        largest, largest_role = _largest_message(messages)

        working = [dict(m) for m in messages]
        repair = sanitize_transcript(working)
        if not repair.changed:
            continue

        affected += 1
        after_chars = _transcript_chars(working)
        print(
            f"session {chat_session.id} "
            f"(workflow {chat_session.workflow_id}, org {chat_session.organization_id})\n"
            f"    largest message : {largest:,} chars (role={largest_role}, "
            f"cap={MAX_TOOL_RESULT_CHARS:,})\n"
            f"    transcript      : {before_chars:,} → {after_chars:,} chars\n"
            f"    repairs         : {repair.describe()}"
        )

        if apply:
            await db_client.update_workflow_gen_chat_session(
                chat_session.id,
                organization_id=chat_session.organization_id,
                messages=working,
            )
            print("    written.")
        print()

    if affected == 0:
        print("Nothing to repair — every transcript is within limits.")
    elif apply:
        print(f"Repaired {affected} session(s).")
    else:
        print(f"{affected} session(s) would be repaired. Re-run with --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repaired transcripts. Without this, nothing is modified.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply))
