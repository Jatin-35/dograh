"""Normalise QA output into the report's analysis section.

A QA node's prompt is per agent, so its JSON keys differ between agents. Each
report field therefore lists the key paths it may come from, first match wins,
and the value is normalised to a small fixed vocabulary — that is what lets a
dashboard add up agents whose prompts were written independently.

QA runs per node, so a call can carry several results. They are rolled up to one
value per call: the *last* node that has a value wins (later nodes see the whole
conversation so far), human transfer is true if any node says so, and tags are
the ordered union.
"""

from typing import Any, Iterable, Optional

from loguru import logger

from api.services.call_report.schema import AnalysisSection, NodeAnalysis
from api.services.gen_ai.json_parser import parse_llm_json

# field -> key paths tried in order. Add a path here to support a new prompt's
# naming; nothing else changes.
_FIELD_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "sentiment": (("overall_sentiment",), ("sentiment",)),
    "reason_for_call": (("reason_for_call",), ("query_type",), ("issue_type",)),
    "satisfied": (
        ("customer_experience", "user_satisfied"),
        ("user_satisfied",),
        ("satisfied",),
    ),
    "mood": (("customer_experience", "user_mood"), ("user_mood",), ("mood",)),
    "resolved": (("issue_resolved",), ("resolved",)),
    "human_transfer": (("human_transfer",),),
    "summary": (("summary",),),
    "quality_score": (("call_quality_score",), ("score",)),
}

_SENTIMENTS = frozenset({"positive", "neutral", "negative"})
_MAX_TEXT = 2000
UNKNOWN = "unknown"


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _first(data: dict, paths: Iterable[tuple[str, ...]]) -> Any:
    for path in paths:
        value = _dig(data, path)
        if value is not None:
            return value
    return None


def _norm_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes"):
            return True
        if lowered in ("false", "no"):
            return False
    return None


def _norm_sentiment(value: Any) -> Optional[str]:
    lowered = str(value).strip().lower() if isinstance(value, str) else ""
    return lowered if lowered in _SENTIMENTS else None


def _norm_slug(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    slug = value.strip().lower().replace(" ", "_")
    return slug or None


def _norm_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:_MAX_TEXT] if text else None


def _norm_score(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 10 else None


def _tag_names(tags: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(tags, list):
        return names
    for tag in tags:
        name = (
            tag
            if isinstance(tag, str)
            else tag.get("tag")
            if isinstance(tag, dict)
            else None
        )
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _analyse_node(node_id: str, result: dict) -> Optional[NodeAnalysis]:
    """Build one node's analysis from its stored result, or None if empty."""
    raw_response = result.get("raw_response")
    parsed = parse_llm_json(raw_response if isinstance(raw_response, str) else "")
    # parse_llm_json returns {"raw": text} when nothing parseable was found.
    if not isinstance(parsed, dict) or set(parsed) <= {"raw"}:
        parsed = {}

    # The four keys the QA task persists itself are the fallback when the raw
    # text could not be parsed.
    fallback = {
        "overall_sentiment": result.get("overall_sentiment"),
        "summary": result.get("summary"),
        "call_quality_score": result.get("score"),
        "tags": result.get("tags"),
    }
    source = {**{k: v for k, v in fallback.items() if v is not None}, **parsed}

    node = NodeAnalysis(
        node_id=str(node_id),
        node_name=str(result.get("node_name") or ""),
        sentiment=_norm_sentiment(_first(source, _FIELD_PATHS["sentiment"])),
        satisfied=_norm_bool(_first(source, _FIELD_PATHS["satisfied"])),
        mood=_norm_slug(_first(source, _FIELD_PATHS["mood"])),
        reason_for_call=_norm_slug(_first(source, _FIELD_PATHS["reason_for_call"])),
        resolved=_norm_bool(_first(source, _FIELD_PATHS["resolved"])),
        human_transfer=_norm_bool(_first(source, _FIELD_PATHS["human_transfer"])),
        summary=_norm_text(_first(source, _FIELD_PATHS["summary"])),
        quality_score=_norm_score(_first(source, _FIELD_PATHS["quality_score"])),
        tags=_tag_names(source.get("tags")),
    )

    has_content = any(
        (
            node.sentiment,
            node.satisfied is not None,
            node.mood,
            node.reason_for_call,
            node.resolved is not None,
            node.human_transfer is not None,
            node.summary,
            node.quality_score is not None,
            node.tags,
        )
    )
    return node if has_content else None


def _last_known(nodes: list[NodeAnalysis], field: str) -> Any:
    for node in reversed(nodes):
        value = getattr(node, field)
        if value is not None:
            return value
    return None


def extract_analysis(annotations: Optional[dict]) -> AnalysisSection:
    """Roll every QA node result on a run up to one analysis section."""
    section = AnalysisSection()
    if not isinstance(annotations, dict):
        return section

    nodes: list[NodeAnalysis] = []
    skipped_reason: Optional[str] = None
    saw_qa = False

    for key, value in annotations.items():
        if not key.startswith("qa_") or not isinstance(value, dict):
            continue
        saw_qa = True

        if value.get("skipped"):
            skipped_reason = value.get("reason") or skipped_reason
            continue

        node_results = value.get("node_results")
        if not isinstance(node_results, dict) or not node_results:
            continue

        for node_id, node_result in node_results.items():
            if not isinstance(node_result, dict):
                continue
            try:
                node = _analyse_node(node_id, node_result)
            except Exception:  # one malformed node must not lose the others
                logger.warning(f"Could not analyse QA node {node_id}", exc_info=True)
                continue
            if node:
                nodes.append(node)

    if nodes:
        section.status = "analysed"
    elif skipped_reason is not None:
        section.status = "skipped"
        section.skipped_reason = str(skipped_reason)
    elif saw_qa:
        # A QA node ran but yielded nothing usable (it errored, or had no transcript).
        section.status = "error"
    else:
        section.status = "not_run"

    section.nodes = nodes
    if not nodes:
        return section

    for field in (
        "sentiment",
        "satisfied",
        "mood",
        "resolved",
        "summary",
        "quality_score",
    ):
        setattr(section, field, _last_known(nodes, field))

    # A real reason beats "unknown" from a node that did not get that far.
    reasons = [n for n in nodes if n.reason_for_call and n.reason_for_call != UNKNOWN]
    section.reason_for_call = (
        reasons[-1].reason_for_call
        if reasons
        else _last_known(nodes, "reason_for_call")
    )

    transfers = [n.human_transfer for n in nodes if n.human_transfer is not None]
    section.human_transfer = any(transfers) if transfers else None

    for node in nodes:
        for tag in node.tags:
            if tag not in section.tags:
                section.tags.append(tag)

    return section
