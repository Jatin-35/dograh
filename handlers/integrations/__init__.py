"""Integration handlers.

Every module here is imported at startup so its @handler decorators run. Add an
integration by creating a module and importing it below — the registry refuses
duplicate names, so a clash fails at boot rather than at call time.
"""

from handlers.integrations import example_order_lookup  # noqa: F401
from handlers.integrations import think_gas_sap  # noqa: F401

__all__ = ["example_order_lookup", "think_gas_sap"]
