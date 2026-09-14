"""Validation for Code Editor files.

Everything here runs on **save**, not on deploy. Catching a malformed schema
when the user writes it — with a message naming the field — is the difference
between a five-second fix and an agent that silently can't call its tool during
a live call.

The rules encode the mistakes the OpenAI function-calling format actually
invites, in particular putting `required` inside `properties` instead of beside
it. That nests the list where the model never reads it, so every parameter
silently becomes optional and the model starts omitting arguments.
"""

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

ENTRY_POINT_PATH = "all_events_entry_point.py"
FUNCTION_DIR = "function_definitions/"
AGENT_DIR = "agents/"

ENTRY_POINT_FUNCTION = "all_events_handler"

_VALID_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# Keys the platform injects at call time. A schema that declares them would make
# the model supply values that are then overwritten — wasted tokens and a
# confusing prompt.
_RESERVED_PARAM_NAMES = {"function_name", "call"}


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_path(path: str) -> ValidationResult:
    """Paths are a contract, not free text — deploy looks files up by location."""
    result = ValidationResult()
    if not path or path.startswith("/") or path.endswith("/"):
        result.errors.append("Path must be a relative file path.")
        return result
    if ".." in path.split("/"):
        result.errors.append("Path may not contain '..'.")
        return result
    if path == ENTRY_POINT_PATH:
        return result
    if path.startswith(FUNCTION_DIR):
        if not path.endswith(".json"):
            result.errors.append(f"Files in {FUNCTION_DIR} must end with .json")
        return result
    if path.startswith(AGENT_DIR):
        if not path.endswith(".ts"):
            result.errors.append(f"Files in {AGENT_DIR} must end with .ts")
        return result
    if path.endswith(".py"):
        # Helper modules are encouraged — the spec asks for a single router, not
        # a single file, and a 2000-line if/elif is its own failure mode.
        return result
    result.errors.append(
        f"Unsupported path {path!r}. Allowed: {ENTRY_POINT_PATH}, "
        f"{FUNCTION_DIR}*.json, {AGENT_DIR}*.ts, or a *.py helper module."
    )
    return result


def _validate_json_schema(schema: Any, where: str, result: ValidationResult) -> None:
    """Recursive check of a JSON Schema fragment."""
    if not isinstance(schema, dict):
        result.errors.append(f"{where}: must be an object.")
        return

    schema_type = schema.get("type")
    if schema_type is None:
        result.errors.append(f"{where}: missing 'type'.")
    elif schema_type not in _VALID_TYPES:
        result.errors.append(
            f"{where}: type {schema_type!r} is not valid "
            f"(use one of {', '.join(sorted(_VALID_TYPES))})."
        )

    properties = schema.get("properties")
    if schema_type == "object":
        if not isinstance(properties, dict):
            result.errors.append(f"{where}: an object needs a 'properties' object.")
            return
        # The classic mistake: 'required' nested inside 'properties'.
        if "required" in properties:
            result.errors.append(
                f"{where}.properties: 'required' is nested inside 'properties'. "
                "It belongs beside it, at the same level as 'properties' — "
                "otherwise every parameter is treated as optional."
            )
        required = schema.get("required", [])
        if not isinstance(required, list):
            result.errors.append(f"{where}: 'required' must be an array of names.")
        else:
            for name in required:
                if name not in properties:
                    result.errors.append(
                        f"{where}: 'required' lists {name!r}, which is not in 'properties'."
                    )
        for name, prop in properties.items():
            if not isinstance(prop, dict):
                result.errors.append(f"{where}.properties.{name}: must be an object.")
                continue
            if not prop.get("description") and prop.get("type") != "object":
                result.warnings.append(
                    f"{where}.properties.{name}: no description. The model relies "
                    "on this to decide what to put here."
                )
            _validate_json_schema(prop, f"{where}.properties.{name}", result)

    if schema_type == "array":
        items = schema.get("items")
        if items is None:
            result.errors.append(f"{where}: an array needs 'items'.")
        else:
            _validate_json_schema(items, f"{where}.items", result)


def validate_function_definition(path: str, content: str) -> ValidationResult:
    """Validate one function_definitions/*.json file."""
    result = ValidationResult()

    try:
        definition = json.loads(content)
    except json.JSONDecodeError as exc:
        result.errors.append(f"Invalid JSON: {exc.msg} (line {exc.lineno}).")
        return result

    if not isinstance(definition, dict):
        result.errors.append("A function definition must be a JSON object.")
        return result

    name = definition.get("name")
    if not name or not isinstance(name, str):
        result.errors.append("Missing 'name'.")
    else:
        if not _NAME_PATTERN.match(name):
            result.errors.append(
                f"name {name!r} must be snake_case: lowercase letters, digits "
                "and underscores, starting with a letter."
            )
        expected = path[len(FUNCTION_DIR):-len(".json")]
        if name != expected:
            # Deploy looks these up by filename; a mismatch means the tool is
            # registered under one name and routed under another.
            result.errors.append(
                f"Filename and name disagree: file is {expected!r}.json but "
                f"'name' is {name!r}. They must match exactly."
            )

    if not definition.get("description"):
        result.errors.append(
            "Missing 'description'. The model reads this to decide whether to "
            "call the function, so it is not optional in practice."
        )

    parameters = definition.get("parameters")
    if parameters is None:
        result.errors.append("Missing 'parameters'.")
    else:
        _validate_json_schema(parameters, "parameters", result)
        if isinstance(parameters, dict) and isinstance(parameters.get("properties"), dict):
            for reserved in _RESERVED_PARAM_NAMES & set(parameters["properties"]):
                result.errors.append(
                    f"parameters.properties.{reserved}: this key is injected "
                    "automatically at call time. Remove it — declaring it makes "
                    "the model supply a value that is then overwritten."
                )

    if definition.get("strict") and isinstance(parameters, dict):
        if parameters.get("additionalProperties") is not False:
            result.errors.append(
                "'strict': true requires 'additionalProperties': false in parameters."
            )

    unknown = set(definition) - {"name", "description", "strict", "parameters", "type"}
    for key in sorted(unknown):
        result.warnings.append(f"Unknown top-level key {key!r} will be ignored.")

    return result


def validate_entry_point(content: str) -> ValidationResult:
    """Check the router parses and exposes the expected handler."""
    result = ValidationResult()
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        result.errors.append(f"Python syntax error on line {exc.lineno}: {exc.msg}")
        return result

    handlers = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == ENTRY_POINT_FUNCTION
    ]
    if not handlers:
        result.errors.append(
            f"{ENTRY_POINT_PATH} must define a top-level "
            f"{ENTRY_POINT_FUNCTION}(event, context) function."
        )
        return result

    args = [a.arg for a in handlers[0].args.args]
    if len(args) < 2:
        result.errors.append(
            f"{ENTRY_POINT_FUNCTION} must accept (event, context); "
            f"found ({', '.join(args)})."
        )
    return result


def validate_file(path: str, content: str) -> ValidationResult:
    """Validate any workspace file by its location."""
    result = validate_path(path)
    if not result.ok:
        return result
    if path == ENTRY_POINT_PATH:
        return validate_entry_point(content)
    if path.startswith(FUNCTION_DIR):
        return validate_function_definition(path, content)
    if path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            result.errors.append(f"Python syntax error on line {exc.lineno}: {exc.msg}")
    # agents/*.ts are validated by the existing Node bridge on deploy, which
    # already owns the SDK grammar — duplicating it here would let the two drift.
    return result
