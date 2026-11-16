"""Langfuse v4 readiness: private traces, project-scoped links, v4 ingestion.

Visibility cases are ported from upstream Dograh's
test_langfuse_trace_visibility.py; the rest cover our v4 additions.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from api.scripts.fix_langfuse_trace_urls import plan_rewrites
from api.services.pipecat import langfuse_visibility, tracing_config
from pipecat.utils.run_context import set_current_org_id

ORG = "org-42"
CREDS = {
    "host": "https://langfuse.example.com",
    "public_key": "pk-lf-test",
    "secret_key": "sk-lf-test",
}


@pytest.fixture
def exporter():
    """Install a routing exporter with no default destination."""
    routing = tracing_config._OrgRoutingExporter(default_exporter=None)
    with patch.object(tracing_config, "_org_routing_exporter", routing):
        yield routing


@pytest.fixture(autouse=True)
def clear_org_context():
    yield
    set_current_org_id(None)


def resolve(env_flag):
    env = {} if env_flag is None else {"LANGFUSE_TRACES_PUBLIC": env_flag}
    with patch.dict(os.environ, env, clear=True):
        return tracing_config._resolve_trace_public()


# --- visibility (upstream cases) -------------------------------------------


def test_org_project_honours_its_own_opt_in(exporter):
    exporter.register_org(ORG, **CREDS, traces_public=True)
    set_current_org_id(ORG)
    assert resolve(None) is True


def test_org_project_ignores_the_env_flag(exporter):
    exporter.register_org(ORG, **CREDS, traces_public=False)
    set_current_org_id(ORG)
    assert resolve("true") is False


def test_org_project_is_private_unless_asked(exporter):
    exporter.register_org(ORG, **CREDS)
    set_current_org_id(ORG)
    assert resolve("true") is False


def test_visibility_toggle_lands_without_a_credential_change(exporter):
    exporter.register_org(ORG, **CREDS, traces_public=True)
    exporter.register_org(ORG, **CREDS, traces_public=False)
    set_current_org_id(ORG)
    assert resolve(None) is False


def test_org_without_own_project_follows_the_env_flag(exporter):
    set_current_org_id(ORG)
    assert resolve("true") is True
    assert resolve(None) is False


def test_unregistering_hands_the_org_back_to_the_env_flag(exporter):
    exporter.register_org(ORG, **CREDS, traces_public=False)
    exporter.unregister_org(ORG)
    set_current_org_id(ORG)
    assert resolve("true") is True


def test_visibility_agrees_with_routing(exporter):
    exporter.register_org(ORG, **CREDS, traces_public=True)
    for org_id, own_project in ((ORG, True), ("other-org", False)):
        set_current_org_id(org_id)
        assert exporter.has_org(org_id) is own_project
        assert resolve(None) is own_project


# --- visibility applied to Pipecat ------------------------------------------


@pytest.fixture
def no_resolver():
    langfuse_visibility.set_trace_public_resolver(None)
    yield
    langfuse_visibility.set_trace_public_resolver(None)


def test_traces_are_private_by_default(no_resolver):
    span = MagicMock()
    with patch.dict(os.environ, {}, clear=True):
        langfuse_visibility.mark_trace_public(span)
    span.set_attribute.assert_not_called()


def test_env_opt_in_marks_public(no_resolver):
    span = MagicMock()
    with patch.dict(os.environ, {"LANGFUSE_TRACES_PUBLIC": "true"}, clear=True):
        langfuse_visibility.mark_trace_public(span)
    span.set_attribute.assert_called_once_with("langfuse.trace.public", True)


def test_failing_resolver_keeps_trace_private(no_resolver):
    def boom():
        raise RuntimeError("x")

    langfuse_visibility.set_trace_public_resolver(boom)
    span = MagicMock()
    langfuse_visibility.mark_trace_public(span)
    span.set_attribute.assert_not_called()


def test_install_replaces_pipecat_marking_everywhere():
    import pipecat.utils.tracing.langfuse_helpers as helpers
    import pipecat.utils.tracing.service_decorators as decorators
    import pipecat.utils.tracing.turn_trace_observer as observer

    langfuse_visibility.install()
    for module in (helpers, decorators, observer):
        assert module.mark_trace_public is langfuse_visibility.mark_trace_public


# --- v4 ingestion -----------------------------------------------------------


def test_org_exporter_sends_v4_ingestion_header(exporter):
    exporter.register_org(ORG, **CREDS)
    headers = exporter._org_exporters[ORG]._headers
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert headers["Authorization"].startswith("Basic ")


def test_reregistering_identical_settings_keeps_the_exporter(exporter):
    exporter.register_org(ORG, **CREDS)
    first = exporter._org_exporters[ORG]
    exporter.register_org(ORG, **CREDS, project_id="p1")
    assert exporter._org_exporters[ORG] is first
    assert exporter.get_org_project_id(ORG) == "p1"


def test_trace_level_attributes_reach_every_span():
    provider = TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(tracing_config._TraceAttributePropagator())
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("conversation") as root:
        root.set_attribute("langfuse.trace.name", "call-7")
        root.set_attribute("langfuse.user.id", "u1")
        root.set_attribute("langfuse.trace.metadata.agent", "vectus")
        root.set_attribute("conversation.id", "c1")
        with tracer.start_as_current_span("turn"):
            with tracer.start_as_current_span("llm"):
                pass

    spans = {s.name: s for s in memory.get_finished_spans()}
    for name in ("turn", "llm"):
        attrs = spans[name].attributes
        assert attrs["langfuse.trace.name"] == "call-7"
        assert attrs["langfuse.user.id"] == "u1"
        assert attrs["langfuse.trace.metadata.agent"] == "vectus"
        assert "conversation.id" not in attrs


# --- links ------------------------------------------------------------------


def test_normalize_host_strips_pasted_page_urls():
    assert (
        tracing_config.normalize_langfuse_host(
            " https://cloud.langfuse.com/project/abc/traces/ "
        )
        == "https://cloud.langfuse.com"
    )
    assert tracing_config.normalize_langfuse_host(None) == ""


def test_trace_url_is_project_scoped_when_project_known(exporter):
    exporter.register_org(ORG, **CREDS, project_id="proj1")
    assert (
        tracing_config.get_trace_url("abc123", org_id=ORG)
        == "https://langfuse.example.com/project/proj1/traces/abc123"
    )


def test_trace_url_falls_back_to_legacy_without_project(exporter):
    exporter.register_org(ORG, **CREDS)
    assert (
        tracing_config.get_trace_url("abc123", org_id=ORG)
        == "https://langfuse.example.com/trace/abc123"
    )


def _fake_httpx(status, payload):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_project_id_lookup():
    client = _fake_httpx(200, {"data": [{"id": "proj9", "name": "Botrix"}]})
    with patch.object(tracing_config.httpx, "AsyncClient", return_value=client):
        project = await tracing_config.fetch_langfuse_project_id(
            "https://cloud.langfuse.com/project/x", "pk", "sk"
        )
    assert project == "proj9"
    url = client.get.call_args.args[0]
    assert url == "https://cloud.langfuse.com/api/public/projects"


@pytest.mark.asyncio
async def test_project_id_lookup_never_raises():
    client = _fake_httpx(401, {})
    with patch.object(tracing_config.httpx, "AsyncClient", return_value=client):
        assert await tracing_config.fetch_langfuse_project_id("h", "pk", "sk") is None
    assert await tracing_config.fetch_langfuse_project_id("", "pk", "sk") is None


# --- repair script ----------------------------------------------------------


def test_repair_plan_rewrites_only_known_projects():
    rows = [
        {
            "id": 1,
            "trace_url": "https://cloud.langfuse.com/trace/aa11",
            "organization_id": 5,
        },
        {
            "id": 2,
            "trace_url": "https://other.example/trace/bb22",
            "organization_id": 5,
        },
        {"id": 3, "trace_url": "https://self.example/trace/cc33", "organization_id": 9},
        {"id": 4, "trace_url": "not a url", "organization_id": 5},
    ]
    projects = {5: ("https://cloud.langfuse.com", "p5")}
    planned, unresolved = plan_rewrites(rows, projects, "https://self.example", "pd")
    assert planned == [
        (1, "https://cloud.langfuse.com/project/p5/traces/aa11"),
        (3, "https://self.example/project/pd/traces/cc33"),
    ]
    assert unresolved == 1
