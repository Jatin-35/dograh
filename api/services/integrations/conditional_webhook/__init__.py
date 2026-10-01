from __future__ import annotations

from api.services.integrations.base import IntegrationPackageSpec
from api.services.integrations.registry import register_package

from .completion import run_completion
from .node import NODE

PACKAGE = register_package(
    IntegrationPackageSpec(
        name="conditional_webhook",
        nodes=(NODE,),
        run_completion=run_completion,
    )
)

__all__ = ["PACKAGE"]
