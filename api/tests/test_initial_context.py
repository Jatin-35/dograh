from api.services.workflow.initial_context import merge_external_initial_context


def test_external_context_cannot_replace_reserved_run_metadata():
    initial_context = {
        "provider": "twilio",
        "runtime_configuration": {"llm_model": "gpt-4.1"},
        "mps_correlation_id": "run-correlation-id",
        "customer_name": "Before",
    }

    merged = merge_external_initial_context(
        initial_context,
        {
            "provider": "external-provider",
            "runtime_configuration": {"llm_model": "external-model"},
            "mps_correlation_id": "external-correlation-id",
            "customer_name": "After",
        },
    )

    assert merged == {
        "provider": "twilio",
        "runtime_configuration": {"llm_model": "gpt-4.1"},
        "mps_correlation_id": "run-correlation-id",
        "customer_name": "After",
    }


def test_external_context_cannot_introduce_reserved_run_metadata():
    assert merge_external_initial_context(
        {},
        {
            "provider": "external-provider",
            "runtime_configuration": {"llm_model": "external-model"},
            "mps_correlation_id": "external-correlation-id",
            "customer_name": "Ada",
        },
    ) == {"customer_name": "Ada"}


def test_external_context_cannot_supply_run_identifiers_or_model_settings():
    merged = merge_external_initial_context(
        {"call_id": "real-call"},
        {
            "call_id": "forged",
            "workflow_run_id": 999,
            "model_overrides": {"llm": "x"},
            "model_configuration_snapshot": {"llm": "x"},
            "model_configuration_overrides": {"llm": "x"},
            "lead_id": "L-1",
        },
    )

    assert merged == {"call_id": "real-call", "lead_id": "L-1"}
