"""Tenant isolation for the credential every generated Code Editor tool uses.

`ensure_runtime_credential` looks its credential up by a fixed name so a deploy
doesn't mint a fresh API key every time. The name is a convenience, not a proof
of contents: a credential is editable by any org admin (and by the assistant's
`create_credential`), so a user who belongs to two organizations can put this
organization's name on a key minted in the other one.

That matters because `/code-editor/run/{function_name}` derives the whole
execution identity from this key — it is what picks which organization's
deployed snapshot runs and whose secrets get decrypted into the sandbox. A
credential named for org A but carrying org B's key means org A's live calls
silently execute org B's code, with org B's secrets, and hand the result back
into org A's transcript.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.code_editor.deploy import (
    RUNTIME_CREDENTIAL_NAME,
    ensure_runtime_credential,
)

ORG_ID = 5
OTHER_ORG_ID = 99


def _credential(uuid: str, *, name: str = RUNTIME_CREDENTIAL_NAME, key: str = "sk-org-5"):
    return SimpleNamespace(
        credential_uuid=uuid,
        name=name,
        credential_data={"header_name": "X-API-Key", "header_value": key},
    )


def _db(*, credentials: list, key_org: int | None):
    db = MagicMock()
    db.get_credentials_for_organization = AsyncMock(return_value=credentials)
    db.validate_api_key = AsyncMock(
        return_value=(
            None if key_org is None else SimpleNamespace(organization_id=key_org)
        )
    )
    db.create_api_key = AsyncMock(
        return_value=(SimpleNamespace(id=1), "sk-freshly-minted")
    )
    db.create_credential = AsyncMock(
        return_value=SimpleNamespace(credential_uuid="cred-new")
    )
    db.update_credential = AsyncMock(return_value=SimpleNamespace())
    return db


@pytest.mark.asyncio
async def test_a_credential_carrying_this_orgs_key_is_reused():
    """The ordinary path — no new API key on every deploy."""
    db = _db(credentials=[_credential("cred-1")], key_org=ORG_ID)

    with patch("api.services.code_editor.deploy.db_client", db):
        uuid = await ensure_runtime_credential(ORG_ID, user_id=1)

    assert uuid == "cred-1"
    db.create_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_credential_carrying_another_orgs_key_is_not_trusted():
    """The isolation failure. Right name, wrong organization behind the key:
    reusing it would run the other org's code with the other org's secrets."""
    db = _db(credentials=[_credential("cred-hijacked")], key_org=OTHER_ORG_ID)

    with patch("api.services.code_editor.deploy.db_client", db):
        uuid = await ensure_runtime_credential(ORG_ID, user_id=1)

    # A key belonging to this org was minted instead of adopting theirs.
    db.create_api_key.assert_awaited_once()
    assert db.create_api_key.await_args.kwargs["organization_id"] == ORG_ID

    # And it replaced the credential in place rather than leaving a second one
    # of the same name beside it — tools reference it by uuid.
    db.update_credential.assert_awaited_once()
    kwargs = db.update_credential.await_args.kwargs
    assert kwargs["credential_uuid"] == "cred-hijacked"
    assert kwargs["organization_id"] == ORG_ID
    assert kwargs["credential_data"]["header_value"] == "sk-freshly-minted"
    assert uuid == "cred-hijacked"
    db.create_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_credential_whose_key_no_longer_exists_is_re_provisioned():
    """A revoked or deleted API key leaves a credential that authenticates as
    nobody. Every generated tool would 401 during a live call."""
    db = _db(credentials=[_credential("cred-dead")], key_org=None)

    with patch("api.services.code_editor.deploy.db_client", db):
        uuid = await ensure_runtime_credential(ORG_ID, user_id=1)

    db.create_api_key.assert_awaited_once()
    assert uuid == "cred-dead"


@pytest.mark.asyncio
async def test_a_credential_with_no_key_at_all_is_re_provisioned():
    """Hand-edited to empty, or saved under a different credential type."""
    blank = _credential("cred-blank")
    blank.credential_data = {}
    db = _db(credentials=[blank], key_org=ORG_ID)

    with patch("api.services.code_editor.deploy.db_client", db):
        await ensure_runtime_credential(ORG_ID, user_id=1)

    db.create_api_key.assert_awaited_once()
    # The key was never even looked up — there was nothing to look up.
    db.validate_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_named_this_yet_provisions_a_fresh_pair():
    """First deploy for an organization: key and credential together."""
    db = _db(credentials=[_credential("other", name="Some other credential")], key_org=ORG_ID)

    with patch("api.services.code_editor.deploy.db_client", db):
        uuid = await ensure_runtime_credential(ORG_ID, user_id=1)

    db.create_api_key.assert_awaited_once()
    db.create_credential.assert_awaited_once()
    db.update_credential.assert_not_awaited()
    assert uuid == "cred-new"
    # An unrelated credential's key is nobody's business to validate.
    db.validate_api_key.assert_not_awaited()
