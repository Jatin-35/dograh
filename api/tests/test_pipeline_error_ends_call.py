"""Which non-fatal pipeline errors still end the call.

Pipecat pushes non-fatal ErrorFrames while it reconnects or retries, and those
must not hang up on the caller. A provider refusing the account never
recovers, though, so it still ends the call instead of leaving silence.
"""

from pipecat.frames.frames import ErrorFrame

from api.services.campaign.circuit_breaker import TOOL_ERROR_PREFIX
from api.services.pipecat.event_handlers import _is_provider_account_rejection


def test_a_reconnect_blip_does_not_end_the_call():
    frame = ErrorFrame("Connection error (failure 1/3): connection reset", fatal=False)
    assert not _is_provider_account_rejection(frame)


def test_a_rejected_key_ends_the_call():
    assert _is_provider_account_rejection(
        ErrorFrame("Error code: 401 - invalid api key", fatal=False)
    )


def test_exhausted_credit_ends_the_call():
    assert _is_provider_account_rejection(
        ErrorFrame("insufficient_quota: you exceeded your current quota", fatal=False)
    )


def test_a_tools_own_401_does_not_end_the_call():
    frame = ErrorFrame(f"{TOOL_ERROR_PREFIX} lsq_update: HTTP 401", fatal=False)
    assert not _is_provider_account_rejection(frame)
