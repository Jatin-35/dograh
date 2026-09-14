"""Handler registry.

A decorator instead of an ``if/elif`` router. The reference platform this
mirrors routes every call through one ``all_events_entry_point`` function, which
becomes unreadable past a handful of integrations and lets two handlers for the
same name silently shadow each other.

Registering by decorator keeps each integration in its own module, and a
duplicate name fails loudly at import rather than quietly at call time.

**The schema lives with the code.** A handler declares the parameters the model
must supply right next to the function that consumes them, and `sync_handler_tools`
turns that declaration into the Dograh tool. That deliberately differs from
keeping schemas in separate ``function_definitions/*.json`` files, where the
filename has to match the function name by convention and the two drift the
moment someone renames one and not the other. Here they cannot disagree,
because there is only one of them.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Kept below whatever timeout_ms the generated Dograh tool carries, so this
# service always fails first and can return something the agent can say.
DEFAULT_TOOL_TIMEOUT_MS = 10_000


@dataclass
class Parameter:
    """A value the model must supply when it calls the tool."""

    name: str
    type: str
    description: str
    required: bool = True

    def as_dograh(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
        }


@dataclass
class Preset:
    """A value Dograh injects server-side; the model never sees it.

    Use this for constants and for call context. Asking the model to supply a
    fixed value is just a chance for it to supply a different one, and routing
    context through the prompt wastes tokens on something it cannot decide.

    ``value_template`` takes a literal ("voicebot") or a template
    ("{{initial_context.phone_number}}").
    """

    name: str
    value_template: str
    type: str = "string"
    required: bool = True

    def as_dograh(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "value_template": self.value_template,
            "required": self.required,
        }


@dataclass
class HandlerSpec:
    name: str
    func: Handler
    description: str
    parameters: list[Parameter] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS
    custom_message: str | None = None

    def manifest(self) -> dict[str, Any]:
        """The tool definition, in the shape `sync_handler_tools` consumes."""
        return {
            "name": self.name,
            "description": self.description,
            "timeout_ms": self.timeout_ms,
            "custom_message": self.custom_message,
            "parameters": [p.as_dograh() for p in self.parameters],
            "preset_parameters": [p.as_dograh() for p in self.presets],
        }


_HANDLERS: dict[str, HandlerSpec] = {}


def handler(
    name: str,
    *,
    description: str,
    parameters: list[Parameter] | None = None,
    presets: list[Preset] | None = None,
    timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS,
    custom_message: str | None = None,
) -> Callable[[Handler], Handler]:
    """Register an async function as a custom tool.

    `description` is what the model reads when deciding whether to call this —
    write it as instruction to the agent, not documentation for a developer.

    `custom_message` is spoken *before* the request goes out, so it covers the
    latency of a slow upstream. Set it on anything that can take more than a
    second; on a phone call the alternative is silence.
    """

    def decorate(func: Handler) -> Handler:
        if name in _HANDLERS:
            raise ValueError(
                f"Two handlers are registered for {name!r}: "
                f"{_HANDLERS[name].func.__module__} and {func.__module__}"
            )
        _HANDLERS[name] = HandlerSpec(
            name=name,
            func=func,
            description=description,
            parameters=list(parameters or []),
            presets=list(presets or []),
            timeout_ms=timeout_ms,
            custom_message=custom_message,
        )
        return func

    return decorate


def get_handler(name: str) -> Handler | None:
    spec = _HANDLERS.get(name)
    return spec.func if spec else None


def get_spec(name: str) -> HandlerSpec | None:
    return _HANDLERS.get(name)


def handler_names() -> list[str]:
    return sorted(_HANDLERS)


def manifest() -> list[dict[str, Any]]:
    """Every registered tool definition, for the sync command."""
    return [_HANDLERS[name].manifest() for name in handler_names()]
