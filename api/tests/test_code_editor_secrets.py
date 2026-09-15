"""Code Editor secrets, and what happens when the key is absent.

Two production scenarios drive these tests:

* The Code Editor ships to a deployment whose operator has not yet added
  ``CODE_EDITOR_ENCRYPTION_KEY``. That must disable *environment variables*
  and nothing else — a missing optional key must not take the whole feature
  down on the first deploy.
* The key is rotated, or differs between two machines. Old values then cannot
  be decrypted, and a run must degrade rather than fail wholesale.

The opposite failure — quietly storing secrets in plaintext when no key is set
— is the one that would never be noticed, so it is asserted directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

from api.services.code_editor import secrets, workspace

ORG_ID = 11


@pytest.fixture
def key(monkeypatch):
    generated = Fernet.generate_key().decode()
    monkeypatch.setenv(secrets.ENV_KEY_NAME, generated)
    return generated


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv(secrets.ENV_KEY_NAME, raising=False)


# --------------------------------------------------------------------------
# With no key configured
# --------------------------------------------------------------------------


def test_storing_a_secret_without_a_key_fails_loudly(no_key):
    """Never a silent plaintext fallback: that is how a deployment ends up
    holding unencrypted secrets everyone believes are encrypted."""
    with pytest.raises(secrets.SecretsNotConfiguredError):
        secrets.encrypt("super-secret")


def test_is_configured_reports_false_rather_than_raising(no_key):
    """The route calls this to refuse the save with a clear message, instead of
    letting the encrypt call surface as a 500."""
    assert secrets.is_configured() is False


@pytest.mark.parametrize("bad", ["not-a-fernet-key", "短", "aGVsbG8="])
def test_a_malformed_key_is_rejected_as_misconfiguration(monkeypatch, bad):
    """A truncated or mis-pasted key must not read as 'no key' — that would
    silently disable encryption on a deployment that thinks it enabled it."""
    monkeypatch.setenv(secrets.ENV_KEY_NAME, bad)
    assert secrets.is_configured() is False
    with pytest.raises(RuntimeError):
        secrets.encrypt("x")


@pytest.mark.asyncio
async def test_a_run_still_works_when_no_key_is_configured(no_key):
    """The whole point: deploying without the key must leave files, versions,
    deploys and test runs working. Only the variables go missing."""
    rows = [SimpleNamespace(key="SAP_PASSWORD", value_encrypted="gAAAAA-stale")]
    with patch.object(
        workspace.db_client,
        "list_code_editor_env_vars",
        AsyncMock(return_value=rows),
    ):
        assert await workspace.resolve_env(ORG_ID) == {}


# --------------------------------------------------------------------------
# With a key
# --------------------------------------------------------------------------


def test_a_secret_round_trips(key):
    token = secrets.encrypt("hunter2-and-then-some")
    assert token != "hunter2-and-then-some"
    assert secrets.decrypt("PASSWORD", token) == "hunter2-and-then-some"


def test_the_ciphertext_does_not_contain_the_plaintext(key):
    """Cheap, but it is the property that matters and it would catch an
    accidental 'encrypt' that just base64s or returns its input."""
    token = secrets.encrypt("correct-horse-battery-staple")
    assert "correct-horse" not in token


def test_a_value_from_a_different_key_is_reported_not_returned(monkeypatch):
    """Key rotation, or two machines configured differently."""
    monkeypatch.setenv(secrets.ENV_KEY_NAME, Fernet.generate_key().decode())
    token = secrets.encrypt("old-value")
    monkeypatch.setenv(secrets.ENV_KEY_NAME, Fernet.generate_key().decode())
    with pytest.raises(secrets.SecretDecryptionError):
        secrets.decrypt("SAP_PASSWORD", token)


@pytest.mark.asyncio
async def test_one_stale_variable_does_not_lose_the_others(key, monkeypatch):
    """A single entry from before a rotation must not make the whole run look
    like a broken platform."""
    good = secrets.encrypt("still-valid")
    stale = Fernet(Fernet.generate_key()).encrypt(b"from-an-old-key").decode()
    rows = [
        SimpleNamespace(key="GOOD", value_encrypted=good),
        SimpleNamespace(key="STALE", value_encrypted=stale),
    ]
    with patch.object(
        workspace.db_client,
        "list_code_editor_env_vars",
        AsyncMock(return_value=rows),
    ):
        assert await workspace.resolve_env(ORG_ID) == {"GOOD": "still-valid"}


# --------------------------------------------------------------------------
# The UI hint
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        # 14+ characters: six trailing characters.
        ("abcdefghijklmn", "ijklmn"),
        ("sk-abcdefghijklmnop", "klmnop"),
        # 8-13 characters: four.
        ("abcdefghijkl", "ijkl"),
        ("12345678", "5678"),
        # Below 8 characters, even four trailing characters would give away
        # half of a short secret, so nothing is shown at all.
        ("1234567", ""),
        ("short", ""),
        ("", ""),
    ],
)
def test_the_hint_never_reveals_a_short_secret(value, expected):
    assert secrets.hint(value) == expected


def test_the_hint_widens_at_the_length_boundary():
    """13 characters gets 4; 14 gets 6 — the tier actually changes something,
    rather than both branches accidentally producing the same slice."""
    assert secrets.hint("a" * 13) == "aaaa"
    assert secrets.hint("a" * 14) == "aaaaaa"
