"""Measure how long callers wait, after connecting, before the bot speaks.

Recording starts the moment the caller's audio connects (on_client_connected),
and the bot's side is saved as its own track (recordings/<run>/bot.wav). The
silence at the start of that track is therefore exactly how long the caller
heard nothing before the greeting: pipeline start, any pre-call fetch, and the
TTS service's time to first audio together.

Read-only: it downloads recordings to a temp folder and prints a table.

On a deployed host, run it inside the api container:

    sudo docker compose exec api python -m api.scripts.measure_call_opening
    sudo docker compose exec api python -m api.scripts.measure_call_opening --limit 50 --workflow-id 7

Locally, source the backend env first per AGENTS.md.
"""

import argparse
import asyncio
import statistics
import tempfile
import wave
from pathlib import Path

import numpy as np
from sqlalchemy import select

from api.db import db_client
from api.db.models import WorkflowRunModel
from api.services.storage import get_storage_for_backend

# A 20 ms window louder than this counts as the bot speaking (int16 peak).
SPEECH_PEAK = 500
WINDOW_SECS = 0.02


def leading_silence_secs(path: Path) -> float | None:
    with wave.open(str(path), "rb") as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    if channels > 1:
        pcm = pcm[::channels]
    step = max(1, int(rate * WINDOW_SECS))
    for i in range(0, len(pcm) - step + 1, step):
        if np.abs(pcm[i : i + step]).max() > SPEECH_PEAK:
            return i / rate
    return None  # the bot never spoke


async def main(limit: int, workflow_id: int | None) -> None:
    async with db_client.async_session() as session:
        query = (
            select(WorkflowRunModel)
            .where(WorkflowRunModel.is_completed.is_(True))
            .order_by(WorkflowRunModel.id.desc())
            .limit(limit * 3)
        )
        if workflow_id:
            query = query.where(WorkflowRunModel.workflow_id == workflow_id)
        runs = (await session.execute(query)).scalars().all()

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for run in runs:
            bot = ((run.extra or {}).get("recordings") or {}).get("bot")
            if not bot:
                continue
            local = Path(tmp) / f"{run.id}.wav"
            fs = get_storage_for_backend(bot.get("storage_backend") or run.storage_backend)
            if not await fs.adownload_file(bot["storage_key"], str(local)):
                continue
            secs = leading_silence_secs(local)
            if secs is not None:
                rows.append((run.id, run.workflow_id, run.mode, secs))
            if len(rows) >= limit:
                break

    if not rows:
        print("No completed runs with a bot recording found.")
        return
    print(f"{'run':>7} {'workflow':>8} {'mode':<14} opening_secs")
    for run_id, wf, mode, secs in rows:
        print(f"{run_id:>7} {wf:>8} {mode:<14} {secs:6.2f}")
    values = sorted(r[3] for r in rows)
    p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
    print(
        f"\n{len(values)} calls: median {statistics.median(values):.2f}s, "
        f"p90 {p90:.2f}s, max {values[-1]:.2f}s"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--workflow-id", type=int)
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.workflow_id))
