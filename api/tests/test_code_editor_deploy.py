"""Deploying a Code Editor version.

Deploy is where code becomes callable on live calls, so the properties worth
pinning are the destructive ones: it must not half-apply, must not overwrite a
tool it doesn't own, and must actually remove a capability when the file that
defined it is deleted.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.code_editor.deploy import (
    MANAGED_MARKER,
    DeployError,
    build_tool_definition,
    deploy_version,
)

ORG = 7
API_URL = "http://api:8000"


def _schema(name="get_order_status", description="Gets an order", extra_prop=None):
    properties = {
        "order_id": {"type": "string", "description": "The order ID like ORD-12345"}
    }
    if extra_prop:
        properties.update(extra_prop)
    return {
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
            "required": ["order_id"],
        },
    }


def _version(files, number=1):
    return SimpleNamespace(version_number=number, files=files)


def _tool(name, definition, status="active", uuid=None):
    return SimpleNamespace(
        name=name,
        definition=definition,
        description="",
        status=status,
        tool_uuid=uuid or f"uuid-{name}",
    )


def _db(version, tools):
    db = MagicMock()
    db.get_code_editor_version = AsyncMock(return_value=version)
    db.get_tools_for_organization = AsyncMock(return_value=tools)
    db.create_tool = AsyncMock()
    db.update_tool = AsyncMock()
    db.archive_tool = AsyncMock(return_value=True)
    db.mark_code_editor_version_deployed = AsyncMock()
    db.get_credentials_for_organization = AsyncMock(
        return_value=[SimpleNamespace(name="Code Editor runtime", credential_uuid="cred-1")]
    )
    db.create_api_key = AsyncMock(return_value=(None, "raw-key"))
    db.create_credential = AsyncMock(
        return_value=SimpleNamespace(credential_uuid="cred-new")
    )
    return db


async def _deploy(db, number=1):
    with patch("api.services.code_editor.deploy.db_client", db):
        return await deploy_version(ORG, number, api_url=API_URL, user_id=1)


# ---------------------------------------------------------------------------
# The generated tool
# ---------------------------------------------------------------------------


def test_the_tool_calls_a_route_that_actually_exists():
    """The generated URL must resolve against a real registered route.

    This previously pointed at an endpoint on the handlers service that was
    never implemented, so every deployed tool would have 404'd on a live call —
    a shape assertion passed happily while the feature was entirely broken.
    """
    from api.app import app

    definition = build_tool_definition(
        "get_order_status", _schema(), api_url=API_URL, version_number=3
    )
    config = definition["config"]

    path = config["url"].replace(API_URL, "")
    registered = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/code-editor/run/{function_name}" in registered
    assert path == "/api/v1/code-editor/run/get_order_status"
    presets = {p["name"]: p["value_template"] for p in config["preset_parameters"]}
    # Injected, not declared — the model can't choose it and shouldn't spend
    # tokens on it.
    assert presets["function_name"] == "get_order_status"
    assert definition["managed_by"] == MANAGED_MARKER
    assert definition["managed_version"] == 3


def test_schema_properties_become_dograh_parameters():
    definition = build_tool_definition(
        "get_order_status",
        _schema(extra_prop={"note": {"type": "string", "description": "optional note"}}),
        api_url=API_URL,
        version_number=1,
    )
    params = {p["name"]: p for p in definition["config"]["parameters"]}

    assert params["order_id"]["required"] is True
    assert params["order_id"]["description"].startswith("The order ID")
    # Not in `required`, so optional — the list is the source of truth.
    assert params["note"]["required"] is False


# ---------------------------------------------------------------------------
# Creating, updating, leaving alone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_function_creates_a_tool():
    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(_schema())}),
        [],
    )
    report = await _deploy(db)

    assert report.created == ["get_order_status"]
    db.create_tool.assert_awaited_once()
    db.mark_code_editor_version_deployed.assert_awaited_once_with(ORG, 1)


@pytest.mark.asyncio
async def test_an_unchanged_function_is_not_rewritten():
    """Rewriting on every deploy would churn updated_at and make the audit
    trail useless for spotting what actually changed."""
    schema = _schema()
    definition = build_tool_definition(
        "get_order_status",
        schema,
        api_url=API_URL,
        version_number=1,
        credential_uuid="cred-1",
    )
    existing = _tool("get_order_status", definition)
    existing.description = schema["description"]

    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(schema)}),
        [existing],
    )
    report = await _deploy(db)

    assert report.unchanged == ["get_order_status"]
    db.update_tool.assert_not_awaited()
    db.create_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_changed_description_updates_the_tool():
    old = build_tool_definition(
        "get_order_status", _schema(), api_url=API_URL, version_number=1
    )
    existing = _tool("get_order_status", old)
    existing.description = "Gets an order"

    db = _db(
        _version(
            {
                "function_definitions/get_order_status.json": json.dumps(
                    _schema(description="Gets an order and its tracking number")
                )
            },
            number=2,
        ),
        [existing],
    )
    report = await _deploy(db, number=2)

    assert report.updated == ["get_order_status"]
    db.update_tool.assert_awaited_once()


# ---------------------------------------------------------------------------
# Not clobbering things it doesn't own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hand_made_tool_with_the_same_name_is_refused():
    """Silently repointing an existing agent's tool at user code is the worst
    possible outcome here."""
    handmade = _tool("get_order_status", {"schema_version": 1, "config": {}})

    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(_schema())}),
        [handmade],
    )
    with pytest.raises(DeployError) as exc:
        await _deploy(db)

    assert any("not created by Code Editor" in e for e in exc.value.errors)
    db.update_tool.assert_not_awaited()
    db.mark_code_editor_version_deployed.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unmanaged_tool_is_never_archived():
    managed = build_tool_definition(
        "still_here", _schema(name="still_here"), api_url=API_URL, version_number=1
    )
    db = _db(
        _version({"function_definitions/still_here.json": json.dumps(_schema(name="still_here"))}),
        [
            _tool("still_here", managed),
            _tool("handmade_tool", {"schema_version": 1, "config": {}}),
        ],
    )
    report = await _deploy(db)

    assert report.archived == []
    db.archive_tool.assert_not_awaited()


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_function_archives_its_tool():
    """Otherwise the capability survives the file and the model keeps offering
    something that no longer exists."""
    gone = build_tool_definition(
        "old_function", _schema(name="old_function"), api_url=API_URL, version_number=1
    )
    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(_schema())}),
        [_tool("old_function", gone)],
    )
    report = await _deploy(db)

    assert report.archived == ["old_function"]
    db.archive_tool.assert_awaited_once_with("uuid-old_function", ORG)


@pytest.mark.asyncio
async def test_an_already_archived_tool_is_not_archived_again():
    gone = build_tool_definition(
        "old_function", _schema(name="old_function"), api_url=API_URL, version_number=1
    )
    db = _db(_version({}), [_tool("old_function", gone, status="archived")])
    report = await _deploy(db)

    assert report.archived == []
    db.archive_tool.assert_not_awaited()


# ---------------------------------------------------------------------------
# All-or-nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_invalid_definition_stops_the_whole_deploy():
    """A half-applied deploy leaves some tools on the new version and some on
    the old, which is far harder to reason about than a refused one."""
    files = {
        "function_definitions/get_order_status.json": json.dumps(_schema()),
        # required nested inside properties — the classic mistake
        "function_definitions/broken.json": json.dumps(
            {
                "name": "broken",
                "description": "b",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string", "description": "x"}, "required": ["x"]},
                },
            }
        ),
    }
    db = _db(_version(files), [])

    with pytest.raises(DeployError) as exc:
        await _deploy(db)

    assert any("broken.json" in e for e in exc.value.errors)
    db.create_tool.assert_not_awaited()
    db.mark_code_editor_version_deployed.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploying_a_missing_version_fails_cleanly():
    db = _db(None, [])
    with pytest.raises(DeployError):
        await _deploy(db, number=99)


@pytest.mark.asyncio
async def test_non_function_files_are_ignored():
    """The router and agent sources live in the same snapshot but are not tools."""
    files = {
        "all_events_entry_point.py": "def all_events_handler(event, context):\n    return {}\n",
        "agents/support.ts": "export default {}",
        "helpers/util.py": "x = 1",
        "function_definitions/get_order_status.json": json.dumps(_schema()),
    }
    db = _db(_version(files), [])
    report = await _deploy(db)

    assert report.created == ["get_order_status"]


# ---------------------------------------------------------------------------
# The runtime credential
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_generated_tool_carries_auth():
    """Without a credential every generated tool gets 401 mid-call, which is a
    miserable thing to diagnose from a transcript."""
    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(_schema())}),
        [],
    )
    await _deploy(db)

    definition = db.create_tool.await_args.kwargs["definition"]
    assert definition["config"]["credential_uuid"] == "cred-1"


@pytest.mark.asyncio
async def test_an_existing_runtime_credential_is_reused():
    """Minting a fresh API key on every deploy would leave a pile of live keys."""
    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(_schema())}),
        [],
    )
    await _deploy(db)

    db.create_api_key.assert_not_awaited()
    db.create_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_credential_is_provisioned_on_first_deploy():
    db = _db(
        _version({"function_definitions/get_order_status.json": json.dumps(_schema())}),
        [],
    )
    db.get_credentials_for_organization = AsyncMock(return_value=[])
    await _deploy(db)

    db.create_api_key.assert_awaited_once()
    db.create_credential.assert_awaited_once()
    # The key's raw value exists only at creation, so it must be stored now.
    assert db.create_credential.await_args.kwargs["credential_data"]["header_value"] == "raw-key"


@pytest.mark.asyncio
async def test_a_failed_deploy_does_not_leave_a_stray_api_key():
    db = _db(_version({"function_definitions/get_order_status.json": "{not json"}), [])
    db.get_credentials_for_organization = AsyncMock(return_value=[])

    with pytest.raises(DeployError):
        await _deploy(db)

    db.create_api_key.assert_not_awaited()
