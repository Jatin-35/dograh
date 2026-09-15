"""End-to-end checks against a real database, not mocks.

Everything added this session is unit-tested with a mocked `db_client`, which
proves the logic but assumes the database agrees. That assumption is exactly
what a mock cannot check: a column that doesn't exist, a value that doesn't
survive a round-trip, an organization filter that reads correctly but doesn't
actually filter. Each test here drives the real code path against real
Postgres rows.

Two orgs are created throughout rather than one. Tenant isolation is the
property most likely to be wrong and least likely to be noticed — a leak looks
like working software until it's someone else's data.
"""

from __future__ import annotations

import pytest

from api.db.models import OrganizationModel, UserModel

pytestmark = pytest.mark.asyncio


GRAPH = {
    "nodes": [
        {
            "id": "n-start",
            "type": "startCall",
            "position": {"x": 0, "y": 0},
            "data": {"name": "Greeting", "prompt": "Hello there."},
        },
        {
            "id": "n-agenda",
            "type": "agentNode",
            "position": {"x": 200, "y": 0},
            "data": {"name": "Main Agenda", "prompt": "A" * 40_000},
        },
        {
            "id": "n-end",
            "type": "endCall",
            "position": {"x": 400, "y": 0},
            "data": {"name": "Done", "prompt": "Goodbye."},
        },
    ],
    "edges": [
        {
            "id": "e1",
            "source": "n-start",
            "target": "n-agenda",
            "data": {"label": "start", "condition": "greeting done"},
        },
        {
            "id": "e2",
            "source": "n-agenda",
            "target": "n-end",
            "data": {"label": "done", "condition": "conversation complete"},
        },
    ],
}


async def _org(async_session, slug: str):
    org = OrganizationModel(provider_id=f"org-{slug}")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(provider_id=f"user-{slug}", selected_organization_id=org.id)
    async_session.add(user)
    await async_session.flush()
    return org, user


@pytest.fixture
async def two_orgs(async_session):
    """Two complete tenants. Every isolation check below uses both."""
    return await _org(async_session, "alpha"), await _org(async_session, "beta")


@pytest.fixture
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet

    from api.services.code_editor import secrets

    monkeypatch.setenv(secrets.ENV_KEY_NAME, Fernet.generate_key().decode())


# ---------------------------------------------------------------------------
# Targeted node editing, against real stored workflow JSON
# ---------------------------------------------------------------------------


class TestNodeEditingForReal:
    async def test_a_prompt_far_over_the_tool_result_cap_survives_a_round_trip(
        self, db_session, two_orgs
    ):
        """The whole reason these tools exist. A 40,000-character prompt is
        well past MAX_TOOL_RESULT_CHARS (24,000) — the size at which the
        whole-workflow fetch came back shortened — and must come back byte
        for byte."""
        from api.mcp_server.tools.node_edit import get_node_for_user

        (org, user), _ = two_orgs
        workflow = await db_session.create_workflow(
            name="Big Agent",
            workflow_definition=GRAPH,
            user_id=user.id,
            organization_id=org.id,
        )
        user.selected_organization_id = org.id

        result = await get_node_for_user(workflow.id, "n-agenda", user)

        assert result["data"]["prompt"] == "A" * 40_000
        assert len(result["data"]["prompt"]) == 40_000

    async def test_an_edit_persists_and_leaves_every_other_node_untouched(
        self, db_session, two_orgs
    ):
        """The property the whole-document round-trip could not guarantee."""
        from api.mcp_server.tools.node_edit import update_node_for_user

        (org, user), _ = two_orgs
        workflow = await db_session.create_workflow(
            name="Editable",
            workflow_definition=GRAPH,
            user_id=user.id,
            organization_id=org.id,
        )
        user.selected_organization_id = org.id

        result = await update_node_for_user(
            workflow.id, "n-agenda", {"prompt": "Much shorter now."}, user
        )
        assert result["saved"] is True

        # Read it back out of the database, not out of the return value.
        draft = await db_session.get_draft_version(workflow.id)
        nodes = {n["id"]: n for n in draft.workflow_json["nodes"]}
        assert nodes["n-agenda"]["data"]["prompt"] == "Much shorter now."
        # Untouched, including the name on the very node that changed.
        assert nodes["n-agenda"]["data"]["name"] == "Main Agenda"
        assert nodes["n-start"]["data"]["prompt"] == "Hello there."
        assert nodes["n-end"]["data"]["prompt"] == "Goodbye."
        assert len(draft.workflow_json["nodes"]) == 3
        assert len(draft.workflow_json["edges"]) == 2

    async def test_the_published_version_keeps_serving_while_a_draft_is_edited(
        self, db_session, two_orgs
    ):
        """A live call must not see an in-progress edit. This is why the tool
        writes a draft and not the released definition."""
        from api.mcp_server.tools.node_edit import update_node_for_user

        (org, user), _ = two_orgs
        workflow = await db_session.create_workflow(
            name="Live Agent",
            workflow_definition=GRAPH,
            user_id=user.id,
            organization_id=org.id,
        )
        user.selected_organization_id = org.id
        published_before = await db_session.get_workflow(
            workflow.id, organization_id=org.id
        )
        released_id_before = published_before.released_definition_id

        await update_node_for_user(workflow.id, "n-start", {"prompt": "CHANGED"}, user)

        after = await db_session.get_workflow(workflow.id, organization_id=org.id)
        assert after.released_definition_id == released_id_before
        released_nodes = {
            n["id"]: n for n in after.released_definition.workflow_json["nodes"]
        }
        assert released_nodes["n-start"]["data"]["prompt"] == "Hello there."

    async def test_one_org_cannot_read_or_edit_the_others_workflow(
        self, db_session, two_orgs
    ):
        """Tenant isolation, driven through the real org-scoped query."""
        from fastapi import HTTPException

        from api.mcp_server.tools.node_edit import (
            get_node_for_user,
            list_nodes_for_user,
            update_node_for_user,
        )

        (org_a, user_a), (org_b, user_b) = two_orgs
        workflow = await db_session.create_workflow(
            name="Alpha's Agent",
            workflow_definition=GRAPH,
            user_id=user_a.id,
            organization_id=org_a.id,
        )
        user_b.selected_organization_id = org_b.id

        for call in (
            lambda: list_nodes_for_user(workflow.id, user_b),
            lambda: get_node_for_user(workflow.id, "n-agenda", user_b),
            lambda: update_node_for_user(workflow.id, "n-agenda", {"prompt": "x"}, user_b),
        ):
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 404

        # And nothing was written to Alpha's workflow.
        draft = await db_session.get_draft_version(workflow.id)
        assert draft is None

    async def test_an_invalid_edit_writes_nothing_to_the_database(
        self, db_session, two_orgs
    ):
        """Validation is the same as a full save's. A rejected edit must leave
        no draft behind — not a half-written one."""
        from api.mcp_server.tools.node_edit import update_node_for_user

        (org, user), _ = two_orgs
        workflow = await db_session.create_workflow(
            name="Validation",
            workflow_definition=GRAPH,
            user_id=user.id,
            organization_id=org.id,
        )
        user.selected_organization_id = org.id

        result = await update_node_for_user(workflow.id, "n-agenda", {"prompt": ""}, user)

        assert result["saved"] is False
        assert await db_session.get_draft_version(workflow.id) is None


# ---------------------------------------------------------------------------
# Env vars: encryption, length, masking — full round trip
# ---------------------------------------------------------------------------


class TestEnvVarsForReal:
    async def test_the_value_is_encrypted_at_rest_and_never_returned(
        self, db_session, two_orgs, fernet_key
    ):
        """The listing the UI renders must not be able to leak a secret even
        if someone renders every field it returns."""
        from api.services.code_editor import workspace

        (org, _), _ = two_orgs
        secret = "sk-live-do-not-leak-me-1234"

        await workspace.set_env_var(org.id, "ORDERS_API_KEY", secret)
        rows = await workspace.list_env_vars(org.id)
        row = next(r for r in rows if r["key"] == "ORDERS_API_KEY")

        assert secret not in str(row)
        assert row["hint"] == secret[-6:]
        assert row["length"] == len(secret)

    async def test_the_mask_is_exactly_as_long_as_the_real_value(
        self, db_session, two_orgs, fernet_key
    ):
        """What the user asked for: the number of asterisks matches the real
        length. Checked against a value the database actually stored, so the
        backend hint tier and the frontend formatter are verified together."""
        from api.services.code_editor import workspace

        (org, _), _ = two_orgs
        secret = "abcdefghijklmnopqrs"  # 19 chars -> 6-char hint tier

        await workspace.set_env_var(org.id, "SOME_KEY", secret)
        row = next(
            r for r in await workspace.list_env_vars(org.id) if r["key"] == "SOME_KEY"
        )

        # Mirrors ui/src/lib/codeEditor.ts::formatMaskedValue.
        masked = "*" * (row["length"] - len(row["hint"])) + row["hint"]
        assert len(masked) == len(secret) == 19
        assert masked == "*" * 13 + "nopqrs"

    async def test_env_vars_do_not_cross_organizations(
        self, db_session, two_orgs, fernet_key
    ):
        from api.services.code_editor import workspace

        (org_a, _), (org_b, _) = two_orgs
        await workspace.set_env_var(org_a.id, "SHARED_NAME", "alpha-secret-value")
        await workspace.set_env_var(org_b.id, "SHARED_NAME", "beta-secret-value")

        a_rows = {r["key"]: r for r in await workspace.list_env_vars(org_a.id)}
        b_rows = {r["key"]: r for r in await workspace.list_env_vars(org_b.id)}

        assert a_rows["SHARED_NAME"]["hint"] == "alpha-secret-value"[-6:]
        assert b_rows["SHARED_NAME"]["hint"] == "beta-secret-value"[-6:]
        assert a_rows["SHARED_NAME"]["length"] != b_rows["SHARED_NAME"]["length"]

    async def test_the_decrypted_value_reaches_the_sandbox_env_intact(
        self, db_session, two_orgs, fernet_key
    ):
        """Encryption is only useful if it round-trips. A value that encrypts
        but decrypts wrong would fail at call time, not here."""
        from api.services.code_editor import workspace

        (org, _), _ = two_orgs
        secret = "p@ssw0rd/with+special=chars&more"

        await workspace.set_env_var(org.id, "TRICKY", secret)
        resolved = await workspace.resolve_env(org.id)

        assert resolved["TRICKY"] == secret


# ---------------------------------------------------------------------------
# The runtime credential's tenant check, against real API keys
# ---------------------------------------------------------------------------


class TestRuntimeCredentialForReal:
    async def test_a_credential_holding_another_orgs_key_is_replaced(
        self, db_session, two_orgs
    ):
        """The isolation hole, end to end: a credential in Alpha named
        'Code Editor runtime' but carrying Beta's API key must not be adopted.
        Reusing it would run Beta's code with Beta's secrets on Alpha's calls."""
        from api.enums import WebhookCredentialType
        from api.services.code_editor.deploy import (
            RUNTIME_CREDENTIAL_NAME,
            ensure_runtime_credential,
        )

        (org_a, user_a), (org_b, user_b) = two_orgs

        # A real API key belonging to Beta.
        _, beta_raw_key = await db_session.create_api_key(
            organization_id=org_b.id, name="beta key", created_by=user_b.id
        )
        # Planted in Alpha under the name deploy looks for.
        planted = await db_session.create_credential(
            organization_id=org_a.id,
            user_id=user_a.id,
            name=RUNTIME_CREDENTIAL_NAME,
            credential_type=WebhookCredentialType.CUSTOM_HEADER.value,
            credential_data={"header_name": "X-API-Key", "header_value": beta_raw_key},
        )

        uuid = await ensure_runtime_credential(org_a.id, user_id=user_a.id)

        # Same credential row (tools reference it by uuid) …
        assert uuid == planted.credential_uuid
        # … but its key now authenticates as Alpha, not Beta.
        refreshed = await db_session.get_credential_by_uuid(uuid, org_a.id)
        new_key = refreshed.credential_data["header_value"]
        assert new_key != beta_raw_key
        authenticated = await db_session.validate_api_key(new_key)
        assert authenticated.organization_id == org_a.id

    async def test_a_legitimate_credential_is_reused_without_minting_keys(
        self, db_session, two_orgs
    ):
        """The ordinary path must not churn a new API key on every deploy."""
        from api.services.code_editor.deploy import ensure_runtime_credential

        (org, user), _ = two_orgs

        first = await ensure_runtime_credential(org.id, user_id=user.id)
        second = await ensure_runtime_credential(org.id, user_id=user.id)

        assert first == second
        keys = await db_session.get_api_keys_by_organization(org.id)
        assert len(keys) == 1


# ---------------------------------------------------------------------------
# The runtime route's identity check, against real workflows
# ---------------------------------------------------------------------------


class TestRunIdentityForReal:
    async def test_a_workflow_id_from_another_org_is_dropped(
        self, db_session, two_orgs, monkeypatch
    ):
        """Forged call identity, driven through the real ownership query."""
        from api.routes import code_editor as route

        (org_a, user_a), (org_b, user_b) = two_orgs
        beta_workflow = await db_session.create_workflow(
            name="Beta's Agent",
            workflow_definition=GRAPH,
            user_id=user_b.id,
            organization_id=org_b.id,
        )
        user_a.selected_organization_id = org_a.id

        captured = {}

        async def fake_run_test(organization_id, event, **kwargs):
            captured.update(kwargs)
            captured["organization_id"] = organization_id
            return {"statusCode": 200, "result": {"ok": True}}

        async def fake_deployed(_org_id):
            from types import SimpleNamespace

            return SimpleNamespace(files={"all_events_entry_point.py": "x"})

        monkeypatch.setattr(route.workspace, "run_test", fake_run_test)
        monkeypatch.setattr(
            route.db_client, "get_deployed_code_editor_version", fake_deployed
        )

        await route.run_deployed_function(
            "some_function",
            {"_dograh_workflow_id": beta_workflow.id},
            user=user_a,
        )

        assert captured["organization_id"] == org_a.id
        assert captured["workflow_id"] is None

    async def test_the_orgs_own_workflow_id_is_passed_through(
        self, db_session, two_orgs, monkeypatch
    ):
        """The check must not break the legitimate case it exists to protect."""
        from api.routes import code_editor as route

        (org, user), _ = two_orgs
        own = await db_session.create_workflow(
            name="Own Agent",
            workflow_definition=GRAPH,
            user_id=user.id,
            organization_id=org.id,
        )
        user.selected_organization_id = org.id

        captured = {}

        async def fake_run_test(organization_id, event, **kwargs):
            captured.update(kwargs)
            return {"statusCode": 200, "result": {}}

        async def fake_deployed(_org_id):
            from types import SimpleNamespace

            return SimpleNamespace(files={"all_events_entry_point.py": "x"})

        monkeypatch.setattr(route.workspace, "run_test", fake_run_test)
        monkeypatch.setattr(
            route.db_client, "get_deployed_code_editor_version", fake_deployed
        )

        await route.run_deployed_function(
            "some_function", {"_dograh_workflow_id": own.id}, user=user
        )

        assert captured["workflow_id"] == own.id


# ---------------------------------------------------------------------------
# replace_in_node against a real workflow, at production size
# ---------------------------------------------------------------------------


class TestReplaceInNodeForReal:
    """The Vectus case, end to end. A ~29,000-character Main Agenda prompt with
    a product database inside it, where three specific lines must go and
    nothing else may move."""

    PRICING = (
        'If asked about pricing, then acknowledge and reply. Use this exactly: '
        '"Actually, hamare prices region to region vary karta h."'
    )
    WHATSAPP = "Ye details WhatsApp par bhi share ho jayengi."
    DATABASE = "1000L Tank | model A | HDPE\n" * 1000

    def _prompt(self) -> str:
        return (
            "## Persona\nYou are a female, do not use a male persona.\n\n"
            f"{self.PRICING}\n\n"
            f"Ji, aapke area ke Area Manager hain. {self.WHATSAPP}\n\n"
            "## Product database\n" + self.DATABASE
        )

    async def _workflow(self, db_session, org, user):
        graph = {
            "nodes": [
                dict(GRAPH["nodes"][0]),
                {
                    "id": "n-agenda",
                    "type": "agentNode",
                    "position": {"x": 200, "y": 0},
                    "data": {"name": "Main Agenda", "prompt": self._prompt()},
                },
                dict(GRAPH["nodes"][2]),
            ],
            "edges": GRAPH["edges"],
        }
        return await db_session.create_workflow(
            name="Vectus Smart Care",
            workflow_definition=graph,
            user_id=user.id,
            organization_id=org.id,
        )

    async def test_three_cleanups_leave_the_product_database_byte_identical(
        self, db_session, two_orgs
    ):
        from api.mcp_server.tools.node_edit import replace_in_node_for_user

        (org, user), _ = two_orgs
        workflow = await self._workflow(db_session, org, user)
        user.selected_organization_id = org.id
        original_len = len(self._prompt())
        assert original_len > 24_000  # past the cap that broke this

        for old, new in (
            (self.PRICING + "\n\n", ""),
            (" " + self.WHATSAPP, ""),
            ("You are a female, do not use a male persona.", "See the Global Node."),
        ):
            result = await replace_in_node_for_user(
                workflow.id, "Main Agenda", "prompt", old, new, user
            )
            assert result["saved"] is True, result

        draft = await db_session.get_draft_version(workflow.id)
        final = next(
            n for n in draft.workflow_json["nodes"] if n["id"] == "n-agenda"
        )["data"]["prompt"]

        assert self.PRICING not in final
        assert self.WHATSAPP not in final
        assert "See the Global Node." in final
        # The whole point: the database is untouched, every row of it.
        assert self.DATABASE in final
        assert final.count("1000L Tank | model A | HDPE") == 1000
        # And the other nodes are as they were.
        nodes = {n["id"]: n for n in draft.workflow_json["nodes"]}
        assert nodes["n-start"]["data"]["prompt"] == "Hello there."
        assert nodes["n-end"]["data"]["prompt"] == "Goodbye."

    async def test_an_ambiguous_match_writes_nothing_to_the_database(
        self, db_session, two_orgs
    ):
        from api.mcp_server.tools.node_edit import replace_in_node_for_user

        (org, user), _ = two_orgs
        workflow = await self._workflow(db_session, org, user)
        user.selected_organization_id = org.id

        result = await replace_in_node_for_user(
            workflow.id, "Main Agenda", "prompt", "1000L Tank", "X", user
        )

        assert result["saved"] is False
        assert result["error_code"] == "text_not_unique"
        assert await db_session.get_draft_version(workflow.id) is None

    async def test_another_org_cannot_replace_text_in_this_ones_prompt(
        self, db_session, two_orgs
    ):
        from fastapi import HTTPException

        from api.mcp_server.tools.node_edit import replace_in_node_for_user

        (org_a, user_a), (org_b, user_b) = two_orgs
        workflow = await self._workflow(db_session, org_a, user_a)
        user_b.selected_organization_id = org_b.id

        with pytest.raises(HTTPException) as exc:
            await replace_in_node_for_user(
                workflow.id, "Main Agenda", "prompt", self.PRICING, "", user_b
            )

        assert exc.value.status_code == 404
        assert await db_session.get_draft_version(workflow.id) is None
