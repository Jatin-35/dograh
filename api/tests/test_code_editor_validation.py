"""Code Editor file validation.

These run on save. Catching a bad schema when it's written — with a message
naming the field — is the difference between a five-second fix and a tool that
silently fails to fire during a live call.
"""

import json

import pytest

from api.services.code_editor.validation import (
    validate_entry_point,
    validate_file,
    validate_function_definition,
    validate_path,
)

PATH = "function_definitions/get_order_status.json"


def _defn(**overrides):
    base = {
        "name": "get_order_status",
        "description": "Gets the current status of a customer order",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID like ORD-12345",
                }
            },
            "additionalProperties": False,
            "required": ["order_id"],
        },
    }
    base.update(overrides)
    return json.dumps(base)


# --------------------------------------------------------------------------
# The OpenAI schema format
# --------------------------------------------------------------------------


def test_the_reference_example_from_the_spec_is_accepted():
    assert validate_function_definition(PATH, _defn()).ok


def test_required_nested_inside_properties_is_rejected_by_name():
    """The mistake this format invites. Nested there the model never reads it,
    so every parameter silently becomes optional."""
    bad = json.dumps(
        {
            "name": "get_order_status",
            "description": "Gets an order",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "id"},
                    "required": ["order_id"],
                },
            },
        }
    )
    result = validate_function_definition(PATH, bad)
    assert not result.ok
    assert any("nested inside 'properties'" in e for e in result.errors)


def test_a_missing_type_is_rejected():
    bad = json.dumps(
        {
            "name": "get_order_status",
            "description": "Gets an order",
            "parameters": {
                "properties": {"order_id": {"type": "string", "description": "id"}}
            },
        }
    )
    assert not validate_function_definition(PATH, bad).ok


def test_required_naming_an_undeclared_property_is_rejected():
    bad = json.dumps(
        {
            "name": "get_order_status",
            "description": "Gets an order",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "id"}},
                "required": ["customer_id"],
            },
        }
    )
    result = validate_function_definition(PATH, bad)
    assert not result.ok
    assert any("customer_id" in e for e in result.errors)


def test_strict_without_additional_properties_false_is_rejected():
    payload = json.loads(_defn())
    payload["parameters"].pop("additionalProperties")
    result = validate_function_definition(PATH, json.dumps(payload))
    assert not result.ok
    assert any("additionalProperties" in e for e in result.errors)


def test_nested_arrays_are_checked_too():
    bad = json.dumps(
        {
            "name": "plan_motion",
            "description": "Plans a path",
            "parameters": {
                "type": "object",
                "properties": {
                    "obstacles": {"type": "array", "description": "coords"}
                },
                "required": [],
            },
        }
    )
    result = validate_function_definition("function_definitions/plan_motion.json", bad)
    assert not result.ok
    assert any("items" in e for e in result.errors)


# --------------------------------------------------------------------------
# Filename / name agreement
# --------------------------------------------------------------------------


def test_filename_and_name_must_match():
    """Deploy looks these up by filename. A mismatch registers the tool under
    one name and routes it under another."""
    result = validate_function_definition(
        "function_definitions/search_products.json", _defn()
    )
    assert not result.ok
    assert any("Filename and name disagree" in e for e in result.errors)


def test_names_must_be_snake_case():
    result = validate_function_definition(
        "function_definitions/GetOrder.json", _defn(name="GetOrder")
    )
    assert not result.ok
    assert any("snake_case" in e for e in result.errors)


# --------------------------------------------------------------------------
# Auto-injected keys
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reserved", ["function_name", "call"])
def test_declaring_an_auto_injected_key_is_rejected(reserved):
    """The platform overwrites these. Declaring one makes the model supply a
    value that is discarded, and bloats the prompt for nothing."""
    payload = json.loads(_defn())
    payload["parameters"]["properties"][reserved] = {
        "type": "string",
        "description": "x",
    }
    result = validate_function_definition(PATH, json.dumps(payload))
    assert not result.ok
    assert any(reserved in e and "injected automatically" in e for e in result.errors)


def test_a_missing_description_is_an_error_not_a_warning():
    payload = json.loads(_defn())
    payload.pop("description")
    assert not validate_function_definition(PATH, json.dumps(payload)).ok


def test_a_parameter_without_a_description_warns_but_still_saves():
    payload = json.loads(_defn())
    payload["parameters"]["properties"]["order_id"].pop("description")
    result = validate_function_definition(PATH, json.dumps(payload))
    assert result.ok
    assert any("no description" in w for w in result.warnings)


def test_invalid_json_reports_the_line():
    result = validate_function_definition(PATH, '{"name": "x",,}')
    assert not result.ok
    assert "line" in result.errors[0]


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------


def test_a_valid_router_is_accepted():
    assert validate_entry_point(
        "def all_events_handler(event, context):\n    return {}\n"
    ).ok


def test_a_router_with_the_wrong_name_is_rejected():
    result = validate_entry_point("def handler(event, context):\n    return {}\n")
    assert not result.ok
    assert "all_events_handler" in result.errors[0]


def test_a_router_with_too_few_arguments_is_rejected():
    assert not validate_entry_point(
        "def all_events_handler(event):\n    return {}\n"
    ).ok


def test_a_syntax_error_reports_the_line():
    result = validate_entry_point(
        "def all_events_handler(event, context)\n    return {}\n"
    )
    assert not result.ok
    assert "line" in result.errors[0].lower()


def test_an_async_router_is_accepted():
    assert validate_entry_point(
        "async def all_events_handler(event, context):\n    return {}\n"
    ).ok


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "all_events_entry_point.py",
        "function_definitions/get_order.json",
        "agents/support_bot.ts",
        "helpers/sap.py",
    ],
)
def test_supported_paths(path):
    assert validate_path(path).ok


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secrets.py",
        "function_definitions/get_order.txt",
        "agents/bot.json",
        "random.md",
        "",
    ],
)
def test_rejected_paths(path):
    assert not validate_path(path).ok


def test_validate_file_routes_by_location():
    assert not validate_file(PATH, '{"nope": true}').ok
    assert validate_file("helpers/util.py", "x = 1\n").ok
    assert not validate_file("helpers/util.py", "x = (\n").ok
    # agents/*.ts are validated by the existing Node bridge on deploy, which
    # owns the SDK grammar — duplicating it here would let the two drift.
    assert validate_file("agents/bot.ts", "this is not typescript {{{").ok
