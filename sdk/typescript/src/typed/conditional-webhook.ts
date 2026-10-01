// GENERATED — do not edit by hand.
//
// Regenerate with `npm run codegen` against the target Dograh backend.
// Source of truth: the backend's model-backed node-spec catalog served
// from `/api/v1/node-types`.

/**
 * Rules checked against the finished call. With no rules it is always sent, like a normal Webhook.
 */
export interface ConditionalWebhookConditionsRow {
    /**
     * Value from the call to check, e.g. gathered_context.whatsapp_consent or initial_context.phone_number.
     */
    variable: string;
    /**
     * How to test the variable.
     */
    operator: "is_true" | "is_false" | "equals" | "not_equals" | "contains" | "is_empty" | "is_not_empty";
    /**
     * Only for equals / does not equal / contains.
     */
    value?: string;
}
/**
 * Additional HTTP headers to include with the request.
 */
export interface ConditionalWebhookCustom_headersRow {
    /**
     * HTTP header name (e.g., 'X-Source').
     */
    key: string;
    /**
     * Header value (supports {{template_variables}}).
     */
    value: string;
}

/**
 * Send an HTTP request after the call, only if its conditions hold.
 *
 * LLM hint: Like the Webhook node, but sent only when its 'Send only if' rules hold for the finished call. Each rule checks a variable (usually an extracted `gathered_context.*` value or an `initial_context.*` field) with is_true, is_false, equals, not_equals, contains, is_empty or is_not_empty. `condition_match` is 'all' or 'any'. The payload is templated like the Webhook node's. Not connected to other nodes.
 */
export interface ConditionalWebhook {
    type: "conditionalWebhook";
    /**
     * Short identifier shown in the canvas and run logs.
     */
    name?: string;
    /**
     * When false, the webhook is never sent.
     */
    enabled?: boolean;
    /**
     * URL the request is sent to.
     */
    endpoint_url?: string;
    /**
     * HTTP verb used for the outbound request.
     */
    http_method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
    /**
     * Rules checked against the finished call. With no rules it is always sent, like a normal Webhook.
     */
    conditions?: Array<ConditionalWebhookConditionsRow>;
    /**
     * All rules must hold, or any one of them.
     */
    condition_match?: "all" | "any";
    /**
     * Optional credential applied as the Authorization header.
     *
     * LLM hint: Credential UUID from `list_credentials`.
     */
    credential_uuid?: string;
    /**
     * Additional HTTP headers to include with the request.
     */
    custom_headers?: Array<ConditionalWebhookCustom_headersRow>;
    /**
     * JSON body of the request, rendered against the run context: `{{workflow_run_id}}`, `{{gathered_context.foo}}`, `{{initial_context.phone_number | phone_digits}}`, etc.
     */
    payload_template?: Record<string, unknown>;
}

/** Factory — sets `type` for you so you don't repeat the discriminator. */
export function conditionalWebhook(input: Omit<ConditionalWebhook, "type">): ConditionalWebhook {
    return { type: "conditionalWebhook", ...input };
}
