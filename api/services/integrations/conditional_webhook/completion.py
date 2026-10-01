from __future__ import annotations

from typing import Any

from loguru import logger

from api.services.integrations.base import IntegrationCompletionContext
from api.services.workflow.dto import WebhookNodeData

from .conditions import conditions_met
from .node import ConditionalWebhookNodeData


async def run_completion(
    nodes: list[dict[str, Any]],
    context: IntegrationCompletionContext,
) -> dict[str, Any]:
    """Check each Conditional Webhook's rules; send the ones that hold.

    Sending goes through the same durable delivery the Webhook node uses
    (retries, delivery log), keyed by this node's id so a retried run never
    sends twice. The outcome of every node is returned as a run annotation.
    """
    # Lazy: tasks.run_integrations imports the integration registry at load.
    from api.tasks.run_integrations import (
        _build_render_context,
        _enqueue_webhook_delivery,
    )

    render_context = _build_render_context(context.workflow_run, context.public_token)
    results: dict[str, Any] = {}

    for node in nodes:
        node_id = str(node.get("id", "unknown"))
        key = f"conditional_webhook_{node_id}"
        try:
            data = ConditionalWebhookNodeData.model_validate(node.get("data", {}))
        except Exception as exc:
            logger.warning(f"Conditional webhook #{node_id} is invalid, skipping: {exc}")
            results[key] = {"sent": False, "reason": "invalid_configuration"}
            continue

        if not data.enabled:
            results[key] = {"name": data.name, "sent": False, "reason": "disabled"}
            continue

        met, failed = conditions_met(
            data.conditions or [], data.condition_match, render_context
        )
        if not met:
            logger.info(
                f"Conditional webhook '{data.name}' not sent for run "
                f"{context.workflow_run_id}: conditions not met ({'; '.join(failed)})"
            )
            results[key] = {
                "name": data.name,
                "sent": False,
                "reason": "conditions_not_met",
                "failed_conditions": failed,
            }
            continue

        await _enqueue_webhook_delivery(
            webhook_data=WebhookNodeData(
                name=data.name,
                enabled=True,
                http_method=data.http_method,
                endpoint_url=data.endpoint_url,
                credential_uuid=data.credential_uuid,
                custom_headers=data.custom_headers,
                payload_template=data.payload_template,
            ),
            render_context=render_context,
            organization_id=context.organization_id,
            workflow_run_id=context.workflow_run_id,
            webhook_node_id=node_id,
        )
        results[key] = {"name": data.name, "sent": True}

    return results
