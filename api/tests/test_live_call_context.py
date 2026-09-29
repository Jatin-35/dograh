"""The provider's call id reaches tools during a live call.

Seen on 2026-09-29: a SmartFlo transfer tool with the preset
``{{gathered_context.call_id}}`` failed with "Preset parameter 'call_id'
resolved to an empty value" while the caller was on the line, although the
run's saved gathered context had the id from the moment the call was admitted.
"""

from api.services.pipecat.run_pipeline import (
    provider_seeds_live_call_id,
    seed_live_context,
)
from api.services.workflow.tools.custom_tool import _resolve_preset_parameters

RECORDED = {"call_id": "CAGE6-T3-1790689705.39967"}
TRANSFER = {
    "preset_parameters": [
        {
            "name": "call_id",
            "value_template": "{{gathered_context.call_id}}",
            "required": True,
        },
        {"name": "intercom", "value_template": "80001", "required": True},
    ]
}


def test_a_transfer_tools_call_id_preset_resolves_during_the_call():
    live, call_vars = {}, {"caller_number": "+919018737669"}
    seed_live_context(live, call_vars, RECORDED)
    assert _resolve_preset_parameters(TRANSFER, call_vars, live) == {
        "call_id": "CAGE6-T3-1790689705.39967",
        "intercom": "80001",
    }


def test_call_id_is_also_offered_as_a_call_variable():
    live, call_vars = {}, {}
    seed_live_context(live, call_vars, RECORDED)
    template = {"preset_parameters": [{"name": "id", "value_template": "{{call_id}}"}]}
    assert _resolve_preset_parameters(template, call_vars, live) == {
        "id": "CAGE6-T3-1790689705.39967"
    }


def test_values_gathered_during_the_call_are_not_overwritten():
    live = {"call_id": "updated-by-the-call", "nodes_visited": ["start call"]}
    call_vars = {"call_id": "from-initial-context"}
    seed_live_context(live, call_vars, {"call_id": "recorded"})
    assert live == {"call_id": "updated-by-the-call", "nodes_visited": ["start call"]}
    assert call_vars["call_id"] == "from-initial-context"


def test_only_the_call_id_is_carried_over():
    """Nothing else the run recorded (retry tags, provider metadata) enters the
    live context, so the call behaves exactly as before."""
    live, call_vars = {}, {}
    recorded = {
        "call_id": "C1",
        "call_tags": ["retry", "retry_reason_no_answer"],
        "provider": "tata_smartflo",
    }
    seed_live_context(live, call_vars, recorded)
    assert live == {"call_id": "C1"}
    assert call_vars == {"call_id": "C1"}


def test_a_run_with_nothing_recorded_is_unchanged():
    live, call_vars = {}, {"caller_number": "+91x"}
    seed_live_context(live, call_vars, None)
    assert live == {} and call_vars == {"caller_number": "+91x"}


def test_only_smartflo_calls_are_seeded():
    """Every other provider's calls run exactly as before."""
    from api.services.telephony import registry

    assert provider_seeds_live_call_id("tata_smartflo")
    others = [n for n in registry._REGISTRY if n != "tata_smartflo"]
    assert others
    assert not any(provider_seeds_live_call_id(n) for n in others)
    assert not provider_seeds_live_call_id("not_a_provider")
