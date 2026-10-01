from __future__ import annotations

from typing import Any

from loguru import logger

from api.db import db_client
from api.services.integrations.base import IntegrationCompletionContext
from api.services.workflow.dto import WebhookNodeData

from .conditions import conditions_met
from .node import ConditionalWebhookNodeData
from .outcome import annotation_key, mask_url


async def run_completion(
    nodes: list[dict[str, Any]],
    context: IntegrationCompletionContext,
) -> dict[str, Any]:
    """Check each Conditional Webhook's rules; send the ones that hold.

    Sending goes through the same durable delivery the Webhook node uses
    (retries, delivery log), keyed by this node's id so a retried run never
    sends twice.

    Every node leaves a run annotation. Skipped nodes are returned (and stored
    by the caller). A sent node writes its own ``status: queued`` entry before
    it is queued, which the delivery task later turns into delivered / retrying
    / failed (see ``outcome.py``); returning it instead would let the caller's
    write land after a fast delivery and reset it to queued.
    """
    # Lazy: tasks.run_integrations imports the integration registry at load.
    from api.tasks.run_integrations import (
        _build_render_context,
        _build_webhook_payload,
        _enqueue_webhook_delivery,
    )

    render_context = _build_render_context(context.workflow_run, context.public_token)
    results: dict[str, Any] = {}

    for node in nodes:
        node_id = str(node.get("id", "unknown"))
        key = annotation_key(node_id)
        try:
            data = ConditionalWebhookNodeData.model_validate(node.get("data", {}))
        except Exception as exc:
            logger.warning(f"Conditional webhook #{node_id} is invalid, skipping: {exc}")
            results[key] = {
                "status": "not_sent",
                "sent": False,
                "reason": "invalid_configuration",
            }
            continue

        if not data.enabled:
            results[key] = {
                "name": data.name,
                "status": "not_sent",
                "sent": False,
                "reason": "disabled",
            }
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
                "status": "not_sent",
                "sent": False,
                "reason": "conditions_not_met",
                "failed_conditions": failed,
            }
            continue

        webhook_data = WebhookNodeData(
            name=data.name,
            enabled=True,
            http_method=data.http_method,
            endpoint_url=data.endpoint_url,
            credential_uuid=data.credential_uuid,
            custom_headers=data.custom_headers,
            payload_template=data.payload_template,
        )
        # Rendering is deterministic, so this is exactly the body that is sent.
        sent_payload = _build_webhook_payload(
            webhook_data, render_context, add_call_disposition=False
        )
        # Only if absent: a retried run must not reset a delivered outcome.
        await db_client.patch_workflow_run_annotation(
            context.workflow_run_id,
            key,
            {
                "name": data.name,
                "status": "queued",
                "sent": False,
                "request": {
                    "method": (data.http_method or "POST").upper(),
                    "url": mask_url(data.endpoint_url),
                    "payload": sent_payload,
                },
            },
            create=True,
        )
        await _enqueue_webhook_delivery(
            webhook_data=webhook_data,
            render_context=render_context,
            organization_id=context.organization_id,
            workflow_run_id=context.workflow_run_id,
            webhook_node_id=node_id,
            # Sent exactly as written: strict APIs (Meta's WhatsApp API)
            # reject the unknown field the plain Webhook adds.
            add_call_disposition=False,
        )

    return results
