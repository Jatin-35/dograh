"""OpenAI function-calling schemas for `WorkflowGenToolbox` methods.

`create_workflow`/`save_workflow` take SDK **TypeScript source**, matching
the MCP tools they delegate to — the source is parsed and spec-validated by
the Node bridge, so the grammar (not this file) is what constrains node and
edge shape. The grammar itself is taught to the model by
`system_prompt.py`, which reuses `DOGRAH_MCP_INSTRUCTIONS` so there is a
single source of truth for it.

`create_tool` is the exception and keeps a fully structural JSON schema:
it has no TypeScript form, and its shape is small and stable enough to
express directly.

`MUTATING_TOOLS` names the tool calls the agent loop must pause on for
explicit user confirmation rather than executing immediately (see
`agent_loop.py`).
"""

from typing import Any

MUTATING_TOOLS = frozenset(
    {
        "create_workflow",
        "save_workflow",
        "create_tool",
        "update_tool",
        "create_credential",
        # Not a write, but it fires a real request at the user's endpoint —
        # a side effect they should approve, not something to do silently.
        "test_tool",
    }
)

# Argument paths whose values are secrets and must be masked before the
# approval card (and the persisted transcript's preview) is built.
SECRET_ARGUMENT_KEYS = frozenset(
    {"api_key", "token", "password", "header_value", "username"}
)

_TOOL_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "snake_case key the agent fills in at call time."},
        "type": {"type": "string", "enum": ["string", "number", "boolean", "object", "array"]},
        "description": {"type": "string", "description": "Instruction to the agent: what value to provide and when."},
        "required": {"type": "boolean"},
    },
    "required": ["name", "type", "description"],
}

_PRESET_TOOL_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Key injected into the request body."},
        "type": {"type": "string", "enum": ["string", "number", "boolean", "object", "array"]},
        "value_template": {
            "type": "string",
            "description": "Fixed value or template, e.g. '{{initial_context.phone_number}}' or '{{gathered_context.email}}'.",
        },
        "required": {"type": "boolean"},
    },
    "required": ["name", "type", "value_template"],
}

_HTTP_API_TOOL_DEFINITION_SCHEMA = {
    "type": "object",
    "description": "An HTTP API tool.",
    "properties": {
        "type": {"const": "http_api"},
        "config": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "url": {"type": "string", "description": "Full endpoint URL."},
                "headers": {
                    "type": ["object", "null"],
                    "description": "Static NON-SECRET headers only. Never put an API key/token/password value "
                    "here — call list_credentials to reuse an existing credential_uuid, or "
                    "create_credential to store the secret, then reference it via credential_uuid.",
                },
                "credential_uuid": {
                    "type": ["string", "null"],
                    "description": "A credential_uuid from list_credentials, used for request auth.",
                },
                "parameters": {
                    "type": "array",
                    "items": _TOOL_PARAMETER_SCHEMA,
                    "description": "Values the calling agent must supply at call time.",
                },
                "preset_parameters": {
                    "type": "array",
                    "items": _PRESET_TOOL_PARAMETER_SCHEMA,
                    "description": "Fixed or templated values Dograh injects automatically.",
                },
                "timeout_ms": {"type": "integer"},
            },
            "required": ["method", "url"],
        },
    },
    "required": ["type", "config"],
}

_END_CALL_TOOL_DEFINITION_SCHEMA = {
    "type": "object",
    "description": "A tool that ends the call.",
    "properties": {
        "type": {"const": "end_call"},
        "config": {
            "type": "object",
            "properties": {
                "messageType": {"type": "string", "enum": ["none", "custom", "audio"]},
                "customMessage": {"type": ["string", "null"]},
                "audioRecordingId": {"type": ["string", "null"]},
                "endCallReason": {"type": "boolean", "description": "If true, the model must supply a reason."},
                "endCallReasonDescription": {"type": ["string", "null"]},
            },
        },
    },
    "required": ["type"],
}

_TRANSFER_CALL_TOOL_DEFINITION_SCHEMA = {
    "type": "object",
    "description": "A tool that transfers the call to a phone number or SIP endpoint.",
    "properties": {
        "type": {"const": "transfer_call"},
        "config": {
            "type": "object",
            "properties": {
                "destination_source": {"type": "string", "enum": ["static", "dynamic"]},
                "destination": {
                    "type": "string",
                    "description": "Phone number, SIP endpoint, or '{{initial_context.*}}' template. Required when destination_source is 'static'.",
                },
                "messageType": {"type": "string", "enum": ["none", "custom", "audio"]},
                "customMessage": {"type": ["string", "null"]},
                "timeout": {"type": "integer", "description": "Seconds to wait for answer (5-120)."},
                "parameters": {"type": "array", "items": _TOOL_PARAMETER_SCHEMA},
            },
            "required": ["destination_source"],
        },
    },
    "required": ["type", "config"],
}

_CALCULATOR_TOOL_DEFINITION_SCHEMA = {
    "type": "object",
    "description": "A calculator tool. No config needed.",
    "properties": {"type": {"const": "calculator"}},
    "required": ["type"],
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_workflow",
            "description": (
                "Create a brand-new voice-agent workflow from SDK TypeScript source. "
                "Only call after the plan is agreed. The workflow name comes from "
                '`new Workflow({ name: "..." })` in the source and is required.'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Complete SDK TypeScript source using @dograh/sdk.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_workflow",
            "description": (
                "Save an existing workflow as a new draft from SDK TypeScript source. "
                "Always call get_workflow_code first, edit that source, and submit the "
                "complete updated source — patches are not accepted. The published "
                "version keeps serving calls until the draft is published."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "integer"},
                    "code": {
                        "type": "string",
                        "description": "Complete updated SDK TypeScript source.",
                    },
                },
                "required": ["workflow_id", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_credential",
            "description": (
                "Store an API key, token or password as a reusable credential and get back "
                "a credential_uuid to reference from a tool's credential_uuid field. Call "
                "list_credentials first and reuse a matching one rather than creating a "
                "duplicate. Never put the secret in a tool's headers instead of doing this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Short descriptive name, unique within the organization "
                            "(e.g. 'Solis CRM API key')."
                        ),
                    },
                    "credential_type": {
                        "type": "string",
                        "enum": ["api_key", "bearer_token", "basic_auth", "custom_header"],
                        "description": (
                            "Use api_key for a secret sent in a named header. Use "
                            "bearer_token only for `Authorization: Bearer <token>` — store "
                            "the token alone, without the 'Bearer ' prefix, which is added "
                            "automatically. Use basic_auth for username/password."
                        ),
                    },
                    "credential_data": {
                        "type": "object",
                        "description": (
                            "Fields required by credential_type: api_key needs "
                            "{header_name, api_key}; bearer_token needs {token}; basic_auth "
                            "needs {username, password}; custom_header needs "
                            "{header_name, header_value}. header_name must be the exact "
                            "header the API expects, e.g. 'Authorization' or 'X-API-Key'."
                        ),
                        "properties": {
                            "header_name": {"type": "string"},
                            "api_key": {"type": "string"},
                            "token": {"type": "string"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "header_value": {"type": "string"},
                        },
                    },
                    "description": {"type": "string"},
                },
                "required": ["name", "credential_type", "credential_data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workflow_code",
            "description": (
                "Return an existing workflow as editable SDK TypeScript. Call this "
                "before save_workflow so edits build on the current source."
            ),
            "parameters": {
                "type": "object",
                "properties": {"workflow_id": {"type": "integer"}},
                "required": ["workflow_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workflow",
            "description": "Fetch a workflow's current definition by id.",
            "parameters": {
                "type": "object",
                "properties": {"workflow_id": {"type": "integer"}},
                "required": ["workflow_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workflows",
            "description": "List workflows (agents) in this organization.",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": ["string", "null"], "description": "'active' (default), 'archived', or null for all."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_node_types",
            "description": "List every available node type with a brief summary. Call before authoring any workflow.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_type",
            "description": "Fetch the authoring schema (fields, types, required, examples) for one node type.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tools",
            "description": "List reusable tools this org's agents can invoke during a call.",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": ["string", "null"]}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List knowledge-base documents agents can reference.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_credentials",
            "description": "List external credentials available for webhook auth and pre-call fetch.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recordings",
            "description": "List pre-recorded audio files for greetings and transition speech.",
            "parameters": {
                "type": "object",
                "properties": {"workflow_id": {"type": ["integer", "null"]}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_tool",
            "description": (
                "Create a NEW reusable HTTP API, end-call, transfer-call, or calculator "
                "tool. Only for tools that don't exist yet — to change an existing one "
                "(attach a credential, fix a URL, edit parameters) call update_tool "
                "instead, or you'll leave a confusing duplicate behind."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_definition": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Concise, action-oriented display name."},
                            "description": {
                                "type": ["string", "null"],
                                "description": "State exactly when the agent should call this and what it gets back.",
                            },
                            "definition": {
                                "oneOf": [
                                    _HTTP_API_TOOL_DEFINITION_SCHEMA,
                                    _END_CALL_TOOL_DEFINITION_SCHEMA,
                                    _TRANSFER_CALL_TOOL_DEFINITION_SCHEMA,
                                    _CALCULATOR_TOOL_DEFINITION_SCHEMA,
                                ],
                            },
                        },
                        "required": ["name", "definition"],
                    }
                },
                "required": ["tool_definition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tool",
            "description": (
                "Fetch one tool's full definition. Call this before update_tool so your "
                "edit builds on what's actually stored."
            ),
            "parameters": {
                "type": "object",
                "properties": {"tool_uuid": {"type": "string"}},
                "required": ["tool_uuid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_tool",
            "description": (
                "Send a real request through an HTTP API tool and return the live "
                "response, status code and timing. Use it right after creating or "
                "updating one to confirm the URL and credential actually work, and to "
                "see the real response shape before wiring the tool into a node. This "
                "calls the user's endpoint for real, so don't use it on anything that "
                "changes their data unless they asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_uuid": {"type": "string"},
                    "llm_params": {
                        "type": "object",
                        "description": "Sample values for the tool's own parameters.",
                    },
                    "preset_params": {
                        "type": "object",
                        "description": "Sample values for preset/templated parameters.",
                    },
                },
                "required": ["tool_uuid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_tool",
            "description": (
                "Change an existing tool in place — attach a credential, fix a URL, edit "
                "parameters or rename it. Prefer this over create_tool whenever a "
                "suitable tool already exists. Call get_tool first and submit the "
                "complete edited definition; it replaces the stored one wholesale."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_uuid": {
                        "type": "string",
                        "description": "From list_tools or get_tool.",
                    },
                    "tool_definition": {
                        "type": "object",
                        "description": (
                            "The complete replacement definition, in the same shape "
                            "get_tool returns. Omit to change only name/description."
                        ),
                    },
                    "name": {"type": "string", "description": "Only pass if renaming."},
                    "description": {
                        "type": "string",
                        "description": "Only pass if changing the description.",
                    },
                },
                "required": ["tool_uuid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_voice_prompting_guide",
            "description": "Fetch staged voice-prompting guidance. Call before composing or revising any prompt field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": ["string", "null"], "description": "'plan' | 'create' | 'review'"},
                    "topic": {"type": ["string", "null"], "description": "A topic id from a prior briefing."},
                    "node_type": {"type": ["string", "null"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Keyword search over Dograh's documentation.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": "Fetch the full content of one documentation page.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "section": {"type": ["string", "null"]}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_docs",
            "description": "Browse the documentation nav tree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": ["string", "null"]}, "depth": {"type": "integer"}},
            },
        },
    },
]
