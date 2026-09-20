"""What an agent is set up to produce, read from its published definition.

A call report only shows the sections its agent can feed. A ticket section on a
sales agent, or a "not analysed" line on an agent with no QA node, is noise that
reads like a fault. These helpers answer, from the definition the run was pinned
to, whether the agent has a ticket tool and whether it has an active QA node.
"""

import re
from typing import Iterable, Optional

from api.services.call_report.tickets import TICKET_SOURCES


def tool_function_name(tool_name: str) -> str:
    """The name the model (and so the call log) uses for a tool.

    Mirrors the sanitising in ``services/workflow/tools/custom_tool.py``: lower-case,
    anything but letters, digits and underscores becomes an underscore.
    """
    name = re.sub(r"[^a-z0-9_]", "_", tool_name.lower())
    return re.sub(r"_+", "_", name).strip("_")


def _nodes(definition: Optional[dict]) -> list[dict]:
    nodes = definition.get("nodes") if isinstance(definition, dict) else None
    return [node for node in nodes or [] if isinstance(node, dict)]


def tool_uuids_in_definition(definition: Optional[dict]) -> set[str]:
    """Every tool any node of the agent can call."""
    uuids: set[str] = set()
    for node in _nodes(definition):
        data = node.get("data")
        for tool_uuid in (
            data.get("tool_uuids") if isinstance(data, dict) else None
        ) or []:
            if isinstance(tool_uuid, str):
                uuids.add(tool_uuid)
    return uuids


def qa_enabled_in_definition(definition: Optional[dict]) -> bool:
    """Whether the agent has a QA node that is switched on (it is on by default)."""
    for node in _nodes(definition):
        if node.get("type") != "qa":
            continue
        data = node.get("data")
        if not isinstance(data, dict) or data.get("qa_enabled", True):
            return True
    return False


def ticket_create_tool_names() -> frozenset[str]:
    names: set[str] = set()
    for source in TICKET_SOURCES:
        names |= source.create_tools
    return frozenset(names)


def has_ticket_tool(tool_function_names: Iterable[str]) -> bool:
    return bool(ticket_create_tool_names() & set(tool_function_names))
