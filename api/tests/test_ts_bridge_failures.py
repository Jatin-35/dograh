"""Failure modes of the Node TS validator bridge.

Unlike `test_ts_bridge.py`, these never spawn `node` — they stub the
subprocess call to assert that a wedged or missing Node turns into a clean
`TsBridgeError` rather than hanging the request or surfacing a bare OS
error. That matters most on machines without node installed, which is why
these live outside the node-gated module.
"""

from __future__ import annotations

import subprocess

import pytest

from api.mcp_server import ts_bridge


@pytest.mark.asyncio
async def test_wedged_node_process_times_out_instead_of_hanging(monkeypatch):
    def _timeout(request):
        raise subprocess.TimeoutExpired(cmd="node", timeout=ts_bridge._VALIDATOR_TIMEOUT_SECONDS)

    monkeypatch.setattr(ts_bridge, "_run_validator_sync", _timeout)

    with pytest.raises(ts_bridge.TsBridgeError, match="did not respond"):
        await ts_bridge.parse_code("export default workflow();")


@pytest.mark.asyncio
async def test_missing_node_binary_reports_an_actionable_error(monkeypatch):
    def _missing(request):
        raise FileNotFoundError(2, "No such file or directory", "node")

    monkeypatch.setattr(ts_bridge, "_run_validator_sync", _missing)

    with pytest.raises(ts_bridge.TsBridgeError, match="Node.js"):
        await ts_bridge.parse_code("export default workflow();")


def test_validator_invocation_passes_a_timeout(monkeypatch):
    """The timeout must reach `subprocess.run` — without it a hung Node
    process holds the request open forever."""
    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _capture)
    ts_bridge._run_validator_sync({"command": "parse"})

    assert captured.get("timeout") == ts_bridge._VALIDATOR_TIMEOUT_SECONDS
