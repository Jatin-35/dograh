"""The deployment wiring that the application code assumes.

A module-level `os.environ.get(NAME, fallback)` is invisible to every other
test in this suite: the code imports fine, the unit tests pass, and the feature
is dead on the deployed box because nothing ever put NAME in the container.

That happened with the Code Editor. `HANDLERS_URL` falls back to
`http://localhost:8080`, which is correct on a developer's machine and is
nothing at all inside the api container — so every test run and every deployed
tool call would have failed in production while the whole suite stayed green.

These tests read `docker-compose.yaml` and assert the api service is actually
given what the code reads. They are cheap, and they cover the one gap unit
tests structurally cannot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yaml"


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


@pytest.fixture(scope="module")
def api_env(services) -> dict:
    return services["api"]["environment"]


def test_the_compose_file_parses_and_has_the_services_we_expect(services):
    """Guards the fixtures themselves: a renamed service would otherwise make
    every assertion below vanish rather than fail."""
    for name in ("api", "handlers", "ui", "postgres", "redis"):
        assert name in services, f"{name} is missing from docker-compose.yaml"


def test_the_api_is_told_where_the_handlers_service_is(api_env):
    """The bug this file exists for."""
    assert "HANDLERS_URL" in api_env, (
        "The api service has no HANDLERS_URL. The code falls back to "
        "localhost:8080, which inside the api container is nothing, so the "
        "whole Code Editor fails on the deployed box while every test passes."
    )
    assert "handlers:8080" in api_env["HANDLERS_URL"], (
        f"HANDLERS_URL is {api_env['HANDLERS_URL']!r}. It must address the "
        "handlers service by its compose service name on app-network; "
        "localhost resolves to the api container itself."
    )


def test_the_api_knows_its_own_internal_address(api_env):
    """This string is baked into every tool the Code Editor generates, so a
    wrong value is not noticed until a live call invokes one — the slowest
    possible moment to find out."""
    assert "API_INTERNAL_URL" in api_env
    assert "localhost" not in api_env["API_INTERNAL_URL"], (
        "API_INTERNAL_URL must address the api service by name. `localhost` "
        "only resolves for a caller inside the api container, which is true "
        "today by accident rather than by design."
    )


def test_both_sides_of_the_handler_key_are_wired(api_env, services):
    """The API sends this key and the handlers service checks it. Wiring only
    one side yields 401 on every call — with the service demonstrably up and
    healthy, which makes it a slow thing to diagnose."""
    handlers_env = services["handlers"]["environment"]
    assert "HANDLERS_API_KEY" in api_env, "the api service never sends the key"
    assert "HANDLERS_API_KEY" in handlers_env, "the handlers service never checks it"
    assert api_env["HANDLERS_API_KEY"] == handlers_env["HANDLERS_API_KEY"], (
        "Both sides must interpolate the same variable, or the key the API "
        "sends is not the key the handlers service expects."
    )


def test_the_encryption_key_is_offered_to_the_api(api_env):
    """Optional by design — empty disables Code Editor environment variables
    and nothing else — but it has to be *passable*. Without the line there is
    no way to configure it at all short of editing the compose file."""
    assert "CODE_EDITOR_ENCRYPTION_KEY" in api_env


@pytest.mark.parametrize(
    "name", ["HANDLERS_API_KEY", "CODE_EDITOR_ENCRYPTION_KEY"]
)
def test_secrets_are_interpolated_never_hardcoded(api_env, name):
    """A literal value here would be a secret committed to the repository."""
    value = api_env[name]
    assert value.startswith("${") or value == "", (
        f"{name} is hardcoded in docker-compose.yaml as {value!r}. It must be "
        "interpolated from the environment."
    )


def test_the_handlers_service_is_not_published_to_the_host(services):
    """It executes code written by our engineers against org credentials and is
    authenticated only by a shared key. It belongs on the internal network."""
    assert "ports" not in services["handlers"], (
        "The handlers service must not publish a host port — it is reachable "
        "on app-network only, and nginx deliberately does not proxy it."
    )


def test_api_and_handlers_share_a_network(services):
    """`HANDLERS_URL` resolving by service name depends on this."""
    assert set(services["api"]["networks"]) & set(services["handlers"]["networks"])


def test_the_handlers_profile_matches_the_rest_of_the_remote_stack(services):
    """handlers is profile-gated. If it were gated to a profile the deployed
    stack does not enable, it would simply never start — so it must match the
    profile nginx uses, which is what the remote deployment brings up."""
    assert services["handlers"].get("profiles") == services["nginx"].get("profiles")
