"""The plain-language change summary shown on the approval card.

This exists because the first version silently over-reported: diffing a real
workflow against *itself* listed edits to three nodes. The card is what a
user approves on, so a summary that invents changes is worse than none.
"""

from __future__ import annotations

import pytest

from api.services.workflow_gen.agent_loop import _describe_workflow_changes


def node(name: str, **data):
    return {"id": name.lower().replace(" ", "_"), "data": {"name": name, **data}}


def wf(nodes, edge_count: int = 0):
    return {"nodes": nodes, "edges": [{} for _ in range(edge_count)]}


def test_identical_definitions_report_no_change():
    """The regression: this used to list three edited nodes."""
    definition = wf([node("Start Call", prompt="hello"), node("End Call", prompt="bye")], 1)
    assert _describe_workflow_changes(definition, definition) == [
        "No structural change — the source is equivalent."
    ]


def test_reports_only_the_field_that_actually_changed():
    before = wf([node("Start Call", prompt="old", allow_interrupt=True)])
    after = wf([node("Start Call", prompt="new", allow_interrupt=True)])
    changes = _describe_workflow_changes(before, after)
    assert changes == ["Edits “Start Call”: prompt (rewritten)"]


def test_added_and_removed_nodes_are_named():
    before = wf([node("Start Call"), node("Qualify")])
    after = wf([node("Start Call"), node("Transfer")])
    changes = _describe_workflow_changes(before, after)
    assert "Adds “Transfer”" in changes
    assert "Removes “Qualify”" in changes


def test_long_prose_says_rewritten_rather_than_dumping_both_versions():
    before = wf([node("Start Call", prompt="a very long prompt " * 20)])
    after = wf([node("Start Call", prompt="a different very long prompt " * 20)])
    (change,) = _describe_workflow_changes(before, after)
    assert "(rewritten)" in change
    assert "very long prompt" not in change


def test_edge_count_change_is_reported():
    before = wf([node("Start Call")], edge_count=2)
    after = wf([node("Start Call")], edge_count=3)
    assert "Connections: 2 → 3" in _describe_workflow_changes(before, after)


def test_many_edited_fields_are_truncated_rather_than_listed_endlessly():
    before = wf([node("Start Call", a=1, b=1, c=1, d=1, e=1, f=1)])
    after = wf([node("Start Call", a=2, b=2, c=2, d=2, e=2, f=2)])
    (change,) = _describe_workflow_changes(before, after)
    assert "+2 more" in change


def test_a_node_renamed_reads_as_add_plus_remove():
    """Nodes are matched on name, so a rename can't be distinguished from
    replacement — which is honest, and still tells the user what happened."""
    changes = _describe_workflow_changes(wf([node("Old")]), wf([node("New")]))
    assert sorted(changes) == ["Adds “New”", "Removes “Old”"]


@pytest.mark.parametrize("empty", [{}, {"nodes": []}, {"nodes": [], "edges": []}])
def test_empty_or_partial_definitions_do_not_raise(empty):
    assert _describe_workflow_changes(empty, wf([node("Start Call")])) == ["Adds “Start Call”"]
    assert _describe_workflow_changes(wf([node("Start Call")]), empty) == ["Removes “Start Call”"]


def test_nodes_without_a_name_fall_back_to_their_id():
    before = {"nodes": [{"id": "n1", "data": {"prompt": "x"}}], "edges": []}
    after = {"nodes": [{"id": "n1", "data": {"prompt": "y"}}], "edges": []}
    (change,) = _describe_workflow_changes(before, after)
    assert "“n1”" in change
