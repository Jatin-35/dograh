"""Register the handlers service's tools in Dograh.

Each handler declares its own schema next to the code that implements it. This
reads that manifest and creates or updates the matching Dograh tool, so adding
an integration never means re-typing a parameter list into the UI and getting
one description subtly wrong.

Runs inside the api container, where the database is reachable:

    # See what would change; writes nothing.
    sudo docker compose exec api python -m api.scripts.sync_handler_tools --org 1

    # Apply it.
    sudo docker compose exec api python -m api.scripts.sync_handler_tools --org 1 --apply

Tools are per-organization, so pass the org that should get them. Run it once
per org that needs these integrations.

`--credential` attaches a stored credential to the generated tools. Create one
in the UI holding the `X-Handler-Key` header, and pass its uuid — without it the
handlers service will reject the calls with 401.
"""

import argparse
import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from api.db import db_client

HANDLERS_URL = os.environ.get("HANDLERS_URL", "http://handlers:8080")


def _fetch_manifest() -> list[dict[str, Any]]:
    key = os.environ.get("HANDLERS_API_KEY", "")
    if not key:
        raise SystemExit("HANDLERS_API_KEY is not set in this container's environment.")
    request = urllib.request.Request(
        f"{HANDLERS_URL}/_manifest", headers={"X-Handler-Key": key}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())["tools"]
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"handlers service returned HTTP {exc.code}. "
            "Check HANDLERS_API_KEY matches on both services."
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Could not reach the handlers service at {HANDLERS_URL}: {exc.reason}. "
            "Is it running? `docker compose --profile remote up -d handlers`"
        ) from exc


def _definition(spec: dict[str, Any], credential_uuid: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "method": "POST",
        "url": f"{HANDLERS_URL}/tools/{spec['name']}",
        "timeout_ms": spec["timeout_ms"],
        "parameters": spec["parameters"],
        "preset_parameters": spec["preset_parameters"],
    }
    if credential_uuid:
        config["credential_uuid"] = credential_uuid
    if spec.get("custom_message"):
        config["customMessage"] = spec["custom_message"]
        config["customMessageType"] = "text"
    return {"schema_version": 1, "category": "http_api", "config": config}


async def main(organization_id: int, credential_uuid: str | None, apply: bool) -> None:
    specs = _fetch_manifest()
    existing = {
        tool.name: tool
        for tool in await db_client.get_tools_for_organization(organization_id)
    }

    print(f"{len(specs)} handler tool(s) in the manifest, org {organization_id}\n")

    created = updated = unchanged = 0
    for spec in specs:
        name = spec["name"]
        definition = _definition(spec, credential_uuid)
        tool = existing.get(name)

        if tool is None:
            print(f"  CREATE  {name}")
            created += 1
            if apply:
                await db_client.create_tool(
                    organization_id=organization_id,
                    user_id=None,
                    name=name,
                    definition=definition,
                    category="http_api",
                    description=spec["description"],
                )
        elif tool.definition != definition or tool.description != spec["description"]:
            print(f"  UPDATE  {name}")
            updated += 1
            if apply:
                await db_client.update_tool(
                    tool_uuid=tool.tool_uuid,
                    organization_id=organization_id,
                    name=name,
                    definition=definition,
                    description=spec["description"],
                )
        else:
            print(f"  ok      {name}")
            unchanged += 1

    print(f"\ncreate={created} update={updated} unchanged={unchanged}")
    if not apply and (created or updated):
        print("Nothing written. Re-run with --apply.")
    if apply and not credential_uuid:
        print(
            "\nWARNING: no --credential given, so these tools have no auth header.\n"
            "The handlers service will reject them with 401. Create a credential\n"
            "holding X-Handler-Key in the UI and re-run with --credential <uuid>."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=int, required=True, help="Organization id.")
    parser.add_argument("--credential", help="Credential uuid holding X-Handler-Key.")
    parser.add_argument("--apply", action="store_true", help="Write the changes.")
    args = parser.parse_args()
    asyncio.run(main(args.org, args.credential, args.apply))
