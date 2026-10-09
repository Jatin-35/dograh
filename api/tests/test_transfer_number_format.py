"""A Transfer Call tool can choose how its number is sent.

VoiceLink reduced every transfer number to 10 digits, so a landline that only
connects with its 0 (Bengaluru 08043061549 → 8043061549) could never be
reached, whatever format was typed. The tool's ``number_format`` sends the
number in the exact shape its line needs; "auto" keeps today's behaviour.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.schemas.tool import TransferCallConfig
from api.services.telephony.number_format import NUMBER_FORMATS, format_transfer_number

LANDLINE = "08043061549"

# (typed, format, sent as-is; None = left to the provider). The UI preview
# (ui/src/lib/transferNumberFormat.test.ts) checks the same cases.
CASES = [
    (LANDLINE, "auto", None),
    (LANDLINE, "keep_zero", "08043061549"),
    ("8043061549", "keep_zero", "08043061549"),
    ("+91 80 4306 1549", "keep_zero", "08043061549"),
    ("918043061549", "keep_zero", "08043061549"),
    (LANDLINE, "with_91", "918043061549"),
    (LANDLINE, "with_plus_91", "+918043061549"),
    ("080-4306-1549", "as_typed", "08043061549"),
    ("9876543210", "with_plus_91", "+919876543210"),
    ("+919876543210", "keep_zero", "09876543210"),
    ("18002026666", "keep_zero", "18002026666"),  # toll-free: no prefix applies
    ("18002026666", "as_typed", "18002026666"),
    ("PJSIP/1234", "keep_zero", None),  # SIP endpoints never touched
    ("{{initial_context.transfer_destination}}", "keep_zero", None),
]


@pytest.mark.parametrize("typed, fmt, sent", CASES)
def test_numbers_are_sent_in_the_chosen_shape(typed, fmt, sent):
    assert format_transfer_number(typed, fmt) == sent


def test_nothing_changes_for_existing_tools():
    assert TransferCallConfig().number_format == "auto"
    assert format_transfer_number(LANDLINE, None) is None
    assert format_transfer_number(LANDLINE, "auto") is None


def test_the_setting_is_saved_with_the_tool():
    for fmt in NUMBER_FORMATS:
        assert TransferCallConfig(destination=LANDLINE, number_format=fmt).number_format == fmt
    with pytest.raises(ValueError):
        TransferCallConfig(destination=LANDLINE, number_format="zero")


async def _voicelink_target(destination: str, **kwargs) -> str:
    """The `target` VoiceLink's transfer event carries for this number."""
    from api.services.telephony.providers.voicelink import provider as vl

    sent = []
    active_call = SimpleNamespace(
        stream_sid="s1",
        call_sid="c1",
        output_transport=SimpleNamespace(queue_frame=AsyncMock(side_effect=lambda f: sent.append(f))),
    )
    p = vl.VoiceLinkProvider.__new__(vl.VoiceLinkProvider)
    with patch.object(vl, "get_active_call", return_value=active_call), patch.object(
        vl.VoiceLinkProvider, "validate_config", return_value=True
    ):
        await p.transfer_call(
            destination=destination, transfer_id="t1", conference_name="transfer-c1", **kwargs
        )
    return sent[0].message["target"]


@pytest.mark.asyncio
async def test_voicelink_still_sends_10_digits_by_default():
    assert await _voicelink_target(LANDLINE) == "8043061549"  # unchanged behaviour


@pytest.mark.asyncio
async def test_voicelink_sends_the_chosen_number_untouched():
    exact = format_transfer_number(LANDLINE, "keep_zero")
    assert await _voicelink_target(exact, exact_destination=True) == "08043061549"


async def _run_transfer_tool(config: dict):
    """Drive the real Transfer Call tool handler with a mock provider; return
    (provider.transfer_call kwargs, stored transfer target)."""
    from unittest.mock import Mock

    from api.enums import WorkflowRunMode
    from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
    from api.tests.test_custom_tools import MockToolModel

    engine = Mock()
    engine._workflow_run_id = 1
    engine._call_context_vars = {}
    engine._gathered_context = {}
    engine._fetch_recording_audio = None
    engine._audio_config = SimpleNamespace(transport_out_sample_rate=8000)
    engine._transport_output = SimpleNamespace(queue_frame=AsyncMock())
    engine._get_organization_id = AsyncMock(return_value=1)
    engine.set_mute_pipeline = Mock()
    engine.end_call_with_reason = AsyncMock()

    tool = MockToolModel(
        tool_uuid="transfer-tool-uuid",
        name="Transfer Call",
        description="Transfer the caller",
        category="transfer_call",
        definition={"schema_version": 1, "type": "transfer_call", "config": config},
    )
    handler, _ = CustomToolManager(engine)._create_handler(tool, "transfer_call")

    provider = Mock()
    provider.supports_transfers.return_value = True
    provider.validate_config.return_value = True
    provider.transfer_call = AsyncMock(return_value={"call_sid": "dest"})
    event = Mock()
    event.to_result_dict.return_value = {"status": "failed", "action": "transfer_failed", "reason": "done"}
    transfers = Mock()
    transfers.store_transfer_context = AsyncMock()
    transfers.wait_for_transfer_completion = AsyncMock(return_value=event)
    params = Mock()
    params.arguments = {}
    params.result_callback = AsyncMock()

    run = SimpleNamespace(mode=WorkflowRunMode.TWILIO.value, gathered_context={"call_id": "caller"})
    base = "api.services.workflow.pipecat_engine_custom_tools"
    with (
        patch(f"{base}.db_client.get_workflow_run_by_id", new=AsyncMock(return_value=run)),
        patch(f"{base}.get_telephony_provider_for_run", new=AsyncMock(return_value=provider)),
        patch(f"{base}.get_call_transfer_manager", new=AsyncMock(return_value=transfers)),
        patch(f"{base}.play_audio_loop", new=AsyncMock(return_value=None)),
    ):
        await handler(params)
    stored = transfers.store_transfer_context.await_args_list[0].args[0]
    return provider.transfer_call.await_args.kwargs, stored.target_number


@pytest.mark.asyncio
async def test_the_tool_sends_the_landline_with_its_zero():
    kwargs, stored = await _run_transfer_tool(
        {"destination": LANDLINE, "timeout": 30, "number_format": "keep_zero"}
    )
    assert kwargs["destination"] == "08043061549"
    assert kwargs["exact_destination"] is True
    assert stored == "08043061549"  # the transfer record shows what was dialled


@pytest.mark.asyncio
async def test_a_tool_without_the_setting_behaves_as_before():
    kwargs, _ = await _run_transfer_tool({"destination": LANDLINE, "timeout": 30})
    assert kwargs["destination"] == LANDLINE  # the provider still decides
    assert kwargs["exact_destination"] is False
