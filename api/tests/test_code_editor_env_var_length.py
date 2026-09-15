"""The env var length round-trip, against a real database.

Everything else touching this table is mocked at the unit-test level; this is
the one place that actually persists a row and reads it back, because the
whole point of `value_length` is a real migration on a real column — a mock
would happily accept a shape that a genuine schema mismatch would reject.
"""

from __future__ import annotations

import pytest

from api.services.code_editor import secrets, workspace

pytestmark = pytest.mark.asyncio


@pytest.fixture
def key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv(secrets.ENV_KEY_NAME, Fernet.generate_key().decode())


async def _org_id(db_session) -> int:
    user, _ = await db_session.get_or_create_user_by_provider_id(
        f"test-user-{id(db_session)}"
    )
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"test-org-{id(db_session)}", user.id
    )
    return org.id


async def test_the_length_of_a_new_value_is_persisted_and_read_back(db_session, key):
    organization_id = await _org_id(db_session)
    value = "sk-abcdefghijklmnop"  # 19 characters

    await workspace.set_env_var(organization_id, "ORDERS_API_KEY", value)
    rows = await workspace.list_env_vars(organization_id)

    row = next(r for r in rows if r["key"] == "ORDERS_API_KEY")
    assert row["length"] == len(value)
    # And the hint tier this feeds actually matches — 19 characters is the
    # 6-character tier, not the old flat 4.
    assert row["hint"] == value[-6:]


async def test_replacing_a_value_updates_the_stored_length(db_session, key):
    """The upsert path, not just the insert path — a shorter replacement must
    not leave the old, longer length behind."""
    organization_id = await _org_id(db_session)

    await workspace.set_env_var(organization_id, "ROTATING_KEY", "a-fairly-long-original-value")
    await workspace.set_env_var(organization_id, "ROTATING_KEY", "short")

    rows = await workspace.list_env_vars(organization_id)
    row = next(r for r in rows if r["key"] == "ROTATING_KEY")
    assert row["length"] == len("short")
    assert row["hint"] == ""  # "short" is under 8 characters


async def test_a_row_from_before_this_column_existed_has_no_length(db_session, key):
    """Simulates data that predates the migration: written with the same
    upsert path a pre-migration deployment would have used, minus the new
    argument. Must degrade to null, not crash the listing."""
    organization_id = await _org_id(db_session)

    from api.db import db_client

    await db_client.upsert_code_editor_env_var(
        organization_id,
        "LEGACY_KEY",
        secrets.encrypt("whatever-was-there"),
        secrets.hint("whatever-was-there"),
        # value_length omitted — exactly what a pre-migration call site did.
    )

    rows = await workspace.list_env_vars(organization_id)
    row = next(r for r in rows if r["key"] == "LEGACY_KEY")
    assert row["length"] is None
