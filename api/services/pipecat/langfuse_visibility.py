"""Langfuse trace visibility: private unless explicitly opted in.

Our pinned Pipecat marks every traced span ``langfuse.trace.public``, which
makes a whole call (transcript, caller details, prompts, tool results) readable
by anyone holding its trace URL, with no Langfuse login. Upstream Dograh fixed
this inside its Pipecat fork (#674, eb3c1321); we apply the same behaviour from
the app so the submodule stays untouched.

``install()`` swaps Pipecat's ``mark_trace_public`` for :func:`mark_trace_public`
below in every module that imported it. Visibility is decided per span by a
resolver (installed by ``tracing_config``: an org with its own Langfuse project
uses its own setting, everything else follows ``LANGFUSE_TRACES_PUBLIC``). A
resolver that raises leaves the trace private: visibility fails closed.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable

from loguru import logger

PUBLIC_ATTRIBUTE = "langfuse.trace.public"

# Pipecat modules that call mark_trace_public by its imported name.
_PIPECAT_MODULES = (
    "pipecat.utils.tracing.langfuse_helpers",
    "pipecat.utils.tracing.service_decorators",
    "pipecat.utils.tracing.turn_trace_observer",
)

_resolver: Callable[[], bool] | None = None


def traces_public_from_env() -> bool:
    """The deployment-wide ``LANGFUSE_TRACES_PUBLIC`` opt-in (off by default)."""
    return os.getenv("LANGFUSE_TRACES_PUBLIC", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def set_trace_public_resolver(resolver: Callable[[], bool] | None) -> None:
    """Decide visibility per span; None falls back to the env flag."""
    global _resolver
    _resolver = resolver


def should_mark_public() -> bool:
    resolver = _resolver
    if resolver is None:
        return traces_public_from_env()
    try:
        return bool(resolver())
    except Exception as e:
        logger.warning(f"Trace visibility resolver failed, keeping trace private: {e}")
        return False


def mark_trace_public(span) -> None:
    """Mark the span's trace public only when its destination opted in."""
    if should_mark_public():
        span.set_attribute(PUBLIC_ATTRIBUTE, True)


def install() -> None:
    """Route every Pipecat call of ``mark_trace_public`` through this module."""
    for name in _PIPECAT_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as e:
            logger.debug(f"Trace visibility: {name} not importable: {e}")
            continue
        if hasattr(module, "mark_trace_public"):
            module.mark_trace_public = mark_trace_public
