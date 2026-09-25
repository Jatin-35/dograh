from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.enums import TelephonyCallStatus, WorkflowRunState
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)
from api.tasks.function_names import FunctionNames


@pytest.mark.asyncio
async def test_initialized_no_answer_enqueues_workflow_completion():
    workflow_run = SimpleNamespace(
        id=123,
        campaign_id=None,
        queued_run_id=None,
        state=WorkflowRunState.INITIALIZED.value,
        is_completed=False,
        logs={"telephony_status_callbacks": []},
        gathered_context={"call_tags": ["existing"]},
    )
    status = StatusCallbackRequest(
        call_id="call-123",
        status="No-Answer",
    )

    with (
        patch("api.services.telephony.status_processor.db_client") as mock_db,
        patch(
            "api.services.telephony.status_processor.campaign_call_dispatcher"
        ) as mock_dispatcher,
        patch(
            "api.services.telephony.status_processor.enqueue_job",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
        mock_db.update_workflow_run = AsyncMock()
        mock_dispatcher.release_call_slot = AsyncMock(return_value=True)

        await _process_status_update(123, status)

    log_update = mock_db.update_workflow_run.await_args_list[0].kwargs
    callback_log = log_update["logs"]["telephony_status_callbacks"][0]
    assert callback_log["status"] == "no-answer"
    assert callback_log["call_id"] == "call-123"

    completion_update = mock_db.update_workflow_run.await_args_list[1].kwargs
    assert completion_update["run_id"] == 123
    assert completion_update["is_completed"] is True
    assert completion_update["state"] == WorkflowRunState.COMPLETED.value
    assert completion_update["usage_info"] == {"call_duration_seconds": 0}
    assert completion_update["gathered_context"] == {
        "call_tags": ["existing", "not_connected", "telephony_no-answer"],
        "call_disposition": "no-answer",
        "mapped_call_disposition": "no-answer",
        "call_id": "call-123",
    }
    mock_enqueue.assert_awaited_once_with(
        FunctionNames.RUN_INTEGRATIONS_POST_WORKFLOW_RUN, 123
    )
    mock_dispatcher.release_call_slot.assert_awaited_once_with(123)


@pytest.mark.asyncio
async def test_running_terminal_status_does_not_enqueue_workflow_completion():
    workflow_run = SimpleNamespace(
        id=456,
        campaign_id=None,
        queued_run_id=None,
        state=WorkflowRunState.RUNNING.value,
        is_completed=False,
        logs={"telephony_status_callbacks": []},
        gathered_context={"call_tags": ["not_connected"]},
    )
    status = StatusCallbackRequest(
        call_id="call-456",
        status=TelephonyCallStatus.FAILED,
        duration="7",
    )

    with (
        patch("api.services.telephony.status_processor.db_client") as mock_db,
        patch(
            "api.services.telephony.status_processor.campaign_call_dispatcher"
        ) as mock_dispatcher,
        patch(
            "api.services.telephony.status_processor.enqueue_job",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
        mock_db.update_workflow_run = AsyncMock()
        mock_dispatcher.release_call_slot = AsyncMock(return_value=True)

        await _process_status_update(456, status)

    completion_update = mock_db.update_workflow_run.await_args_list[1].kwargs
    assert "usage_info" not in completion_update
    assert completion_update["gathered_context"]["call_tags"] == [
        "not_connected",
        "telephony_failed",
    ]
    mock_enqueue.assert_not_awaited()
    mock_dispatcher.release_call_slot.assert_awaited_once_with(456)


def _campaign_run(gathered_context):
    return SimpleNamespace(
        id=789,
        campaign_id=42,
        queued_run_id=9,
        state=WorkflowRunState.INITIALIZED.value,
        is_completed=False,
        logs={"telephony_status_callbacks": []},
        gathered_context=gathered_context,
    )


async def _process_campaign_no_answer(workflow_run, *, wins_claim=True):
    publisher = SimpleNamespace(publish_retry_needed=AsyncMock())
    claim = AsyncMock(return_value=wins_claim)
    with (
        patch("api.services.telephony.status_processor.db_client") as mock_db,
        patch(
            "api.services.telephony.status_processor.campaign_call_dispatcher"
        ) as mock_dispatcher,
        patch(
            "api.services.telephony.status_processor.circuit_breaker"
        ) as mock_breaker,
        patch(
            "api.services.telephony.status_processor.get_campaign_event_publisher",
            new_callable=AsyncMock,
            return_value=publisher,
        ),
        patch(
            "api.services.telephony.status_processor.enqueue_job",
            new_callable=AsyncMock,
        ),
        patch(
            "api.services.telephony.status_processor.rate_limiter.claim_not_connected_report",
            claim,
        ),
    ):
        mock_db.get_workflow_run_by_id = AsyncMock(return_value=workflow_run)
        mock_db.update_workflow_run = AsyncMock()
        mock_dispatcher.release_call_slot = AsyncMock(return_value=True)
        mock_breaker.record_and_evaluate = AsyncMock()

        await _process_status_update(
            workflow_run.id,
            StatusCallbackRequest(call_id="call-789", status="no-answer"),
        )

    return publisher, mock_breaker, mock_dispatcher, claim


@pytest.mark.asyncio
async def test_first_campaign_no_answer_schedules_one_retry():
    publisher, breaker, dispatcher, claim = await _process_campaign_no_answer(
        _campaign_run({})
    )

    publisher.publish_retry_needed.assert_awaited_once_with(
        workflow_run_id=789,
        reason="no_answer",
        campaign_id=42,
        queued_run_id=9,
    )
    breaker.record_and_evaluate.assert_awaited_once()
    dispatcher.release_call_slot.assert_awaited_once_with(789)


@pytest.mark.asyncio
async def test_repeated_not_connected_report_schedules_no_second_retry():
    # VoiceLink reports one unanswered call twice (call.failed or call.ended,
    # then call.completed); the second must not queue a duplicate retry or
    # count the call twice toward the circuit breaker.
    already_reported = _campaign_run(
        {"call_tags": ["not_connected", "telephony_no-answer"]}
    )
    already_reported.state = WorkflowRunState.COMPLETED.value
    already_reported.is_completed = True

    publisher, breaker, dispatcher, claim = await _process_campaign_no_answer(
        already_reported
    )

    publisher.publish_retry_needed.assert_not_awaited()
    breaker.record_and_evaluate.assert_not_awaited()
    dispatcher.release_call_slot.assert_awaited_once_with(789)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_report_that_loses_the_claim_schedules_no_retry():
    # Both reports read the run before either tagged it; the atomic claim
    # lets only one of them act.
    publisher, breaker, dispatcher, claim = await _process_campaign_no_answer(
        _campaign_run({}), wins_claim=False
    )

    claim.assert_awaited_once_with(789)
    publisher.publish_retry_needed.assert_not_awaited()
    breaker.record_and_evaluate.assert_not_awaited()
    dispatcher.release_call_slot.assert_awaited_once_with(789)
