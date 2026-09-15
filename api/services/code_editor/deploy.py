"""Deploying a version.

Deploy is the moment code becomes callable. It takes an immutable snapshot and
reconciles the platform against it:

* every ``function_definitions/*.json`` becomes (or updates) a Dograh tool that
  routes to the org's router in the sandbox;
* a tool that used to come from Code Editor and is no longer in the snapshot is
  archived, so deleting a file actually removes the capability rather than
  leaving a stale tool the model can still pick;
* the version is stamped deployed.

Tools created here are marked in their definition with ``managed_by:
code_editor`` and the version they came from. That marker is what lets the
no-code tool UI show "managed in Code Editor" and warn before an edit that the
next deploy would overwrite — the drift trap the reference platform documents
and does not otherwise prevent.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from api.db import db_client
from api.enums import ToolCategory, ToolStatus, WebhookCredentialType
from api.services.code_editor.validation import FUNCTION_DIR, validate_function_definition

logger = logging.getLogger(__name__)

MANAGED_MARKER = "code_editor"
# Comfortably above the sandbox's own limit so the sandbox always fails first
# and can return a structured, speakable error instead of a bare timeout.
DEFAULT_TOOL_TIMEOUT_MS = 15_000

# The name of the credential deploy provisions for itself. Stable, because it is
# looked up by name to avoid minting a new API key on every deploy.
RUNTIME_CREDENTIAL_NAME = "Code Editor runtime"
RUNTIME_API_KEY_NAME = "Code Editor runtime"
# Matches CODE_EDITOR_ICON_COLOR in ui/src/app/tools/config.tsx.
CODE_TOOL_ICON_COLOR = "#CA8A04"


async def _key_belongs_to(credential: Any, organization_id: int) -> bool:
    """Whether this credential's API key authenticates as `organization_id`.

    Resolved the same way the runtime route resolves it — `validate_api_key`,
    then the key's own `organization_id` — so the answer here is exactly what
    a live call would get, rather than a second opinion that could drift from
    it.
    """
    data = credential.credential_data or {}
    raw_key = data.get("header_value")
    if not isinstance(raw_key, str) or not raw_key:
        return False
    api_key = await db_client.validate_api_key(raw_key)
    return bool(api_key) and api_key.organization_id == organization_id


async def ensure_runtime_credential(organization_id: int, user_id: Optional[int]) -> str:
    """The credential a generated tool authenticates with, created on demand.

    The runtime route derives the organization from this key rather than from
    the request body, because a tool's configuration is editable by any org
    admin — trusting an org id in the payload would let one organization execute
    another's code.

    Provisioned automatically rather than documented as a setup step: a missing
    credential would surface as every generated tool returning 401 during a live
    call, which is a miserable thing to debug from a transcript.
    """
    existing = await db_client.get_credentials_for_organization(organization_id)
    stale: Optional[str] = None
    for credential in existing:
        if credential.name != RUNTIME_CREDENTIAL_NAME:
            continue
        # The name is not proof of contents. A credential is editable by any
        # org admin, and a user who belongs to two organizations can put this
        # organization's name on a key minted in the other one. Reusing it on
        # that basis would make every generated tool authenticate as the other
        # org — running *its* deployed code with *its* decrypted secrets, and
        # returning the result into this org's call. So check where the key
        # actually leads before trusting it.
        if await _key_belongs_to(credential, organization_id):
            return credential.credential_uuid
        logger.warning(
            "Credential %r in organization %s does not carry a key for this "
            "organization; re-provisioning it.",
            RUNTIME_CREDENTIAL_NAME,
            organization_id,
        )
        stale = credential.credential_uuid
        break

    # An API key's raw value exists only at creation, so the key and the
    # credential that carries it are created together or not at all.
    _, raw_key = await db_client.create_api_key(
        organization_id=organization_id,
        name=RUNTIME_API_KEY_NAME,
        created_by=user_id,
    )

    if stale is not None:
        # Overwritten in place rather than left beside a second credential of
        # the same name: tools already deployed reference it by uuid, and two
        # identically named credentials would make the next lookup a coin toss.
        await db_client.update_credential(
            credential_uuid=stale,
            organization_id=organization_id,
            credential_type=WebhookCredentialType.CUSTOM_HEADER.value,
            credential_data={"header_name": "X-API-Key", "header_value": raw_key},
        )
        return stale
    credential = await db_client.create_credential(
        organization_id=organization_id,
        user_id=user_id,
        name=RUNTIME_CREDENTIAL_NAME,
        credential_type=WebhookCredentialType.CUSTOM_HEADER.value,
        credential_data={"header_name": "X-API-Key", "header_value": raw_key},
        description=(
            "Lets Code Editor functions call back into the platform. "
            "Managed automatically — do not delete."
        ),
    )
    logger.info(
        "code_editor provisioned runtime credential for org %s", organization_id
    )
    return credential.credential_uuid


@dataclass
class DeployReport:
    version_number: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_number": self.version_number,
            "created": self.created,
            "updated": self.updated,
            "archived": self.archived,
            "unchanged": self.unchanged,
            "errors": self.errors,
        }


class DeployError(Exception):
    def __init__(self, message: str, *, errors: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def _parameters_from_schema(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate an OpenAI parameters block into Dograh's parameter list.

    Dograh stores a flat list of named parameters rather than a JSON Schema, so
    this flattens the top level. Nested objects keep their JSON type; the
    handler receives the value as the model produced it.
    """
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parameters = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        parameters.append(
            {
                "name": name,
                "type": prop.get("type", "string"),
                "description": prop.get("description", ""),
                "required": name in required,
            }
        )
    return parameters


def build_tool_definition(
    function_name: str,
    schema: dict[str, Any],
    *,
    api_url: str,
    version_number: int,
    credential_uuid: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "category": ToolCategory.HTTP_API.value,
        # Not part of Dograh's tool contract — carried so the UI can tell a
        # code-managed tool from a hand-made one and warn before editing it.
        "managed_by": MANAGED_MARKER,
        "managed_version": version_number,
        "config": {
            "method": "POST",
            **({"credential_uuid": credential_uuid} if credential_uuid else {}),
            "url": f"{api_url}/api/v1/code-editor/run/{function_name}",
            "timeout_ms": DEFAULT_TOOL_TIMEOUT_MS,
            "parameters": _parameters_from_schema(schema.get("parameters") or {}),
            # function_name is injected here rather than declared in the schema,
            # so the router can dispatch without the model spending tokens on a
            # value it cannot choose.
            "preset_parameters": [
                {
                    "name": "function_name",
                    "type": "string",
                    "value_template": function_name,
                    "required": True,
                }
            ],
        },
    }


async def deploy_version(
    organization_id: int,
    version_number: int,
    *,
    api_url: str,
    user_id: Optional[int] = None,
) -> DeployReport:
    """Reconcile the org's tools against a version, then stamp it deployed."""
    version = await db_client.get_code_editor_version(organization_id, version_number)
    if version is None:
        raise DeployError(f"Version {version_number} does not exist.")

    files: dict[str, str] = version.files or {}
    report = DeployReport(version_number=version_number)

    # Parse and validate everything before writing anything. A half-applied
    # deploy leaves some tools pointing at the new version and some at the old,
    # which is far harder to reason about than a refused one.
    definitions: dict[str, dict[str, Any]] = {}
    for path, content in sorted(files.items()):
        if not path.startswith(FUNCTION_DIR) or not path.endswith(".json"):
            continue
        result = validate_function_definition(path, content)
        if not result.ok:
            report.errors.extend(f"{path}: {error}" for error in result.errors)
            continue
        schema = json.loads(content)
        definitions[schema["name"]] = schema

    if report.errors:
        raise DeployError(
            "This version cannot be deployed because it has invalid function "
            "definitions.",
            errors=report.errors,
        )

    existing = {
        tool.name: tool
        for tool in await db_client.get_tools_for_organization(organization_id)
    }

    # Only mint a credential once there is something to attach it to, so a
    # deploy that fails validation doesn't leave a stray API key behind.
    credential_uuid = (
        await ensure_runtime_credential(organization_id, user_id) if definitions else None
    )

    for name, schema in definitions.items():
        definition = build_tool_definition(
            name,
            schema,
            api_url=api_url,
            version_number=version_number,
            credential_uuid=credential_uuid,
        )
        description = schema.get("description", "")
        tool = existing.get(name)

        if tool is None:
            await db_client.create_tool(
                organization_id=organization_id,
                user_id=user_id,
                name=name,
                definition=definition,
                category=ToolCategory.HTTP_API.value,
                description=description,
                # Distinguishes these from hand-made HTTP tools in the picker.
                icon="code",
                icon_color=CODE_TOOL_ICON_COLOR,
            )
            report.created.append(name)
            continue

        if (tool.definition or {}).get("managed_by") != MANAGED_MARKER:
            # A hand-made tool already owns this name. Overwriting it would
            # silently repoint an existing agent's tool at user code.
            report.errors.append(
                f"{name}: a tool with this name already exists and was not "
                "created by Code Editor. Rename one of them."
            )
            continue

        unchanged = tool.definition == definition and tool.description == description
        if unchanged:
            report.unchanged.append(name)
            continue

        await db_client.update_tool(
            tool_uuid=tool.tool_uuid,
            organization_id=organization_id,
            definition=definition,
            description=description,
        )
        report.updated.append(name)

    if report.errors:
        raise DeployError("Deploy stopped before writing.", errors=report.errors)

    # A function deleted from the workspace must stop being callable. Archive
    # rather than delete: an agent may still reference the uuid, and archiving
    # removes it from the model's choices without breaking that reference.
    for name, tool in existing.items():
        if name in definitions:
            continue
        if (tool.definition or {}).get("managed_by") != MANAGED_MARKER:
            continue
        if tool.status == ToolStatus.ARCHIVED.value:
            continue
        await db_client.archive_tool(tool.tool_uuid, organization_id)
        report.archived.append(name)

    await db_client.mark_code_editor_version_deployed(organization_id, version_number)
    logger.info(
        "code_editor deployed org=%s version=%s created=%s updated=%s archived=%s",
        organization_id,
        version_number,
        len(report.created),
        len(report.updated),
        len(report.archived),
    )
    return report
