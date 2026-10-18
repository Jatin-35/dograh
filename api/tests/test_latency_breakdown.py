"""The per-turn latency breakdown saved in run logs."""

from pipecat.observers.user_bot_latency_observer import (
    FunctionCallMetrics,
    LatencyBreakdown,
    TextAggregationBreakdownMetrics,
    TTFBBreakdownMetrics,
)

from api.services.pipecat.latency_breakdown import (
    LATENCY_BREAKDOWN_EVENT,
    build_latency_breakdown_event,
)


def test_breakdown_event_keeps_every_part():
    event = build_latency_breakdown_event(
        LatencyBreakdown(
            ttfb=[
                TTFBBreakdownMetrics(processor="SarvamSTT#0", start_time=1.0, duration_secs=0.21234),
                TTFBBreakdownMetrics(
                    processor="OpenAILLMService#0", model="gemma-4", start_time=1.3, duration_secs=0.5
                ),
            ],
            text_aggregation=TextAggregationBreakdownMetrics(
                processor="TTS#0", start_time=1.8, duration_secs=0.1
            ),
            user_turn_start_time=0.5,
            user_turn_secs=0.9,
            function_calls=[
                FunctionCallMetrics(function_name="vectus_tank_lookup", start_time=1.4, duration_secs=0.02)
            ],
        )
    )
    assert event["type"] == LATENCY_BREAKDOWN_EVENT == "rtf-latency-breakdown"
    p = event["payload"]
    assert p["user_turn_secs"] == 0.9
    assert p["ttfb"] == [
        {"processor": "SarvamSTT#0", "model": None, "secs": 0.212},
        {"processor": "OpenAILLMService#0", "model": "gemma-4", "secs": 0.5},
    ]
    assert p["text_aggregation_secs"] == 0.1
    assert p["function_calls"] == [{"name": "vectus_tank_lookup", "secs": 0.02}]


def test_breakdown_event_with_nothing_measured():
    p = build_latency_breakdown_event(LatencyBreakdown())["payload"]
    assert p == {"user_turn_secs": None, "ttfb": [], "text_aggregation_secs": None, "function_calls": []}
