"""Drive the whole Code Editor flow locally and report what happened.

Exercises the same path a user does — seed a workspace, write a function, run
it in the sandbox, snapshot it, deploy it, confirm a real Dograh tool now
exists pointing at a route that resolves — against the real database and the
real sandbox. This is the check that unit tests cannot make, because the bugs
that matter live in the seams between the pieces.

Run the API's dependencies and the sandbox first::

    # terminal 1 — the sandbox
    export HANDLERS_API_KEY=dev-key                 # PowerShell: $env:HANDLERS_API_KEY="dev-key"
    uvicorn handlers.main:app --port 8080

    # terminal 2 — this script
    set -a && source api/.env && set +a
    export HANDLERS_API_KEY=dev-key
    python -m api.scripts.smoke_code_editor --org 1

Nothing here touches production: it writes to whatever DATABASE_URL points at,
so source ``api/.env`` (dev), not a production environment. Pass ``--cleanup``
to remove what it created.
"""

import argparse
import asyncio
import json
import os
import sys

from api.db import db_client
from api.services.code_editor import workspace
from api.services.code_editor.deploy import MANAGED_MARKER, deploy_version

FUNCTION_NAME = "smoke_check_order"
FUNCTION_PATH = f"function_definitions/{FUNCTION_NAME}.json"

ROUTER = '''"""Smoke-test router."""


def all_events_handler(event, context):
    function_name = event.get("function_name")

    if function_name == "smoke_check_order":
        order_id = event.get("order_id")
        print("router received", order_id, "for org", context.get("organization_id"))
        return {
            "status": "shipped",
            "speak": f"Order {order_id} has shipped.",
            "org": context.get("organization_id"),
        }

    return {"error": f"Unknown function: {function_name}"}
'''

SCHEMA = json.dumps(
    {
        "name": FUNCTION_NAME,
        "description": "Smoke test: returns a fixed delivery status for an order.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "e.g. ORD-12345"}
            },
            "additionalProperties": False,
            "required": ["order_id"],
        },
    }
)

PASS = "  PASS"
FAIL = "  FAIL"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}  {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


async def main(organization_id: int, user_id: int | None, cleanup: bool) -> int:
    print(f"\nCode Editor smoke test — org {organization_id}")
    print(f"sandbox: {workspace.HANDLERS_URL}")
    if not os.environ.get("HANDLERS_API_KEY"):
        print("\nHANDLERS_API_KEY is not set. Set the same value here and on the")
        print("sandbox, or every call will be refused with 401.")
        return 2

    print("\n1. workspace")
    files = await workspace.ensure_workspace(organization_id)
    check("starter workspace exists", "all_events_entry_point.py" in files,
          f"{len(files)} file(s)")

    print("\n2. writing files")
    await workspace.save_file(organization_id, "all_events_entry_point.py", ROUTER, user_id)
    await workspace.save_file(organization_id, FUNCTION_PATH, SCHEMA, user_id)
    check("router and schema saved", True)

    try:
        await workspace.save_file(
            organization_id, "function_definitions/wrong_name.json", SCHEMA, user_id
        )
        check("a filename/name mismatch is refused", False, "it was accepted")
    except workspace.WorkspaceError as exc:
        check("a filename/name mismatch is refused", True, exc.errors[0][:70])

    print("\n3. sandbox execution")
    try:
        outcome = await workspace.run_test(
            organization_id,
            {"function_name": FUNCTION_NAME, "order_id": "ORD-SMOKE"},
        )
    except workspace.WorkspaceError as exc:
        check("the sandbox is reachable", False, str(exc))
        return 1

    check("the function ran", outcome.get("statusCode") == 200, f"status {outcome.get('statusCode')}")
    check("it returned the expected result",
          (outcome.get("result") or {}).get("status") == "shipped",
          json.dumps(outcome.get("result"))[:80])
    check("print output was captured", "router received ORD-SMOKE" in (outcome.get("logs") or ""))
    check("call context reached the handler",
          (outcome.get("result") or {}).get("org") == organization_id)

    crash = await workspace.run_test(
        organization_id,
        {"function_name": FUNCTION_NAME},
        files={"all_events_entry_point.py": "def all_events_handler(event, context):\n    raise RuntimeError('boom')\n"},
    )
    check("a crash is reported, not raised", crash.get("statusCode") == 500,
          (crash.get("error") or "")[:60])

    print("\n4. version")
    version = await workspace.create_version(organization_id, "smoke test", user_id)
    check("version created", version.version_number > 0, f"v{version.version_number}")

    print("\n5. deploy")
    report = await deploy_version(
        organization_id,
        version.version_number,
        api_url=os.environ.get("API_INTERNAL_URL", "http://localhost:8000"),
        user_id=user_id,
    )
    check("deploy reported the function",
          FUNCTION_NAME in (report.created + report.updated + report.unchanged),
          json.dumps(report.as_dict())[:90])

    tools = {t.name: t for t in await db_client.get_tools_for_organization(organization_id)}
    tool = tools.get(FUNCTION_NAME)
    check("a Dograh tool now exists", tool is not None)
    if tool:
        config = (tool.definition or {}).get("config", {})
        check("it is marked code-managed",
              (tool.definition or {}).get("managed_by") == MANAGED_MARKER)
        check("it carries an auth credential", bool(config.get("credential_uuid")))
        check("function_name is injected, not asked of the model",
              any(p["name"] == "function_name" for p in config.get("preset_parameters", [])))
        check("order_id is exposed to the model",
              any(p["name"] == "order_id" for p in config.get("parameters", [])),
              config.get("url", ""))

    deployed = await db_client.get_deployed_code_editor_version(organization_id)
    check("the version is marked deployed",
          deployed is not None and deployed.version_number == version.version_number)

    if cleanup:
        print("\n6. cleanup")
        await db_client.delete_code_editor_file(organization_id, FUNCTION_PATH)
        if tool:
            await db_client.archive_tool(tool.tool_uuid, organization_id)
        print("  removed the smoke function and archived its tool")

    print("\n" + ("-" * 58))
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=int, required=True, help="Organization id to test against.")
    parser.add_argument("--user", type=int, default=None, help="User id for audit fields.")
    parser.add_argument("--cleanup", action="store_true", help="Remove what the run created.")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.org, args.user, args.cleanup)))
