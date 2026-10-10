import { client } from "@/client/client.gen";
import { detailFromError } from "@/lib/apiError";
import { encodeFiltersToURL } from "@/lib/filters";
import type { ActiveFilter, DateRangeValue, FilterAttribute, MultiSelectValue, TextValue } from "@/types/filters";

/**
 * Webhook Sync: CRM webhook → lead → call.
 *
 * Thin wrappers over /api/v1/webhook-sync. They call the shared generated
 * `client` directly (same base URL + auth interceptor as every request)
 * because these endpoints were added after the last `npm run generate-client`.
 */

export type AuthType = "api_key" | "hmac" | "url_token";

export type LeadStatus =
    | "received"
    | "on_hold"
    | "skipped"
    | "invalid_number"
    | "duplicate"
    | "queued"
    | "scheduled"
    | "calling"
    | "completed"
    | "no_answer"
    | "busy"
    | "failed"
    | "do_not_call";

export interface FieldMapping {
    phone?: string | null;
    name?: string | null;
    email?: string | null;
    external_lead_id?: string | null;
    source?: string | null;
    city?: string | null;
    language_preference?: string | null;
    custom: Record<string, string>;
    /** Keep only the standard fields and `custom` as call variables. */
    only_mapped?: boolean;
    /** Per field, what the CRM sends → what we store and the agent gets. */
    value_maps?: Record<string, Record<string, string>>;
}

export type StandardField = Exclude<keyof FieldMapping, "custom" | "only_mapped" | "value_maps">;

export interface CallingHours {
    start: string;
    end: string;
    timezone: string;
    days: number[];
}

export interface RetrySettings {
    max_attempts: number;
    gap_minutes: number;
    on_statuses: ("no_answer" | "busy" | "failed")[];
}

export interface CallSettings {
    delay_minutes: number;
    calling_hours: CallingHours;
    retries: RetrySettings;
    dedupe_window_hours: number;
    /** The same CRM lead id within this many days is a repeat; after it, a new enquiry. */
    reenquiry_days?: number;
    callback_url?: string | null;
    /** Which telephony configuration places the calls; the org default when unset. */
    telephony_configuration_id?: number | null;
    /** Who is emailed when the endpoint needs attention; empty means its creator. */
    alert_emails?: string[];
}

export interface WebhookEndpoint {
    id: number;
    name: string;
    endpoint_uuid: string;
    webhook_url: string;
    auth_type: AuthType;
    secret: string;
    workflow_id: number;
    workflow_name?: string | null;
    campaign_id?: number | null;
    /** The calling campaign's state (running / paused); null until the first lead is queued. */
    calling_state?: string | null;
    is_active: boolean;
    auto_call: boolean;
    field_mapping: FieldMapping;
    call_settings: CallSettings;
    rate_limit_per_minute: number;
    leads_today: number;
    leads_total: number;
    /** Leads stored while the endpoint was paused, waiting for someone to choose. */
    leads_on_hold?: number;
    created_at: string;
    updated_at: string;
}

export interface EndpointInput {
    name: string;
    workflow_id: number;
    auth_type: AuthType;
    is_active: boolean;
    auto_call: boolean;
    field_mapping: FieldMapping;
    call_settings: CallSettings;
    rate_limit_per_minute: number;
}

export interface WebhookLead {
    id: number;
    endpoint_id: number;
    external_lead_id?: string | null;
    name?: string | null;
    phone?: string | null;
    phone_raw?: string | null;
    email?: string | null;
    source?: string | null;
    variables: Record<string, unknown>;
    status: LeadStatus | string;
    status_reason?: string | null;
    duplicate_of_lead_id?: number | null;
    call_attempts: number;
    last_call_status?: string | null;
    disposition?: string | null;
    last_workflow_run_id?: number | null;
    next_retry_at?: string | null;
    received_at: string;
    updated_at: string;
    raw_payload?: unknown;
    phone_masked: boolean;
    /** The CRM callback for this lead (single-lead read only). */
    callback?: LeadCallback | null;
}

export interface LeadCallback {
    /** pending (sending / retrying), succeeded, or dead_letter (gave up) */
    status: string;
    attempts: number;
    last_status_code?: number | null;
    last_error?: string | null;
    updated_at?: string | null;
}

export interface SyncStats {
    days: number;
    leads: number;
    callable: number;
    called: number;
    connected: number;
    connect_rate?: number | null;
    median_seconds_to_first_call?: number | null;
    requests: number;
    request_errors: number;
    callbacks: Record<string, number>;
}

export interface LeadStats {
    total: number;
    today: number;
    by_status: Record<string, number>;
}

export interface RequestLog {
    id: number;
    endpoint_id: number;
    method: string;
    content_type?: string | null;
    headers: Record<string, unknown>;
    raw_body?: string | null;
    body_truncated: boolean;
    response_code: number;
    error?: string | null;
    ip?: string | null;
    lead_ids?: number[] | null;
    duration_ms?: number | null;
    received_at: string;
}

export interface AuditEntry {
    id: number;
    endpoint_id?: number | null;
    endpoint_name?: string | null;
    user_id?: number | null;
    user_email?: string | null;
    action: string;
    /** A field diff for "updated"; for "alert": { kind, message, emailed_to }. */
    changes?: Record<string, unknown> | null;
    created_at: string;
}

/** An alert entry's details (action "alert"), or null for any other entry. */
export function alertDetails(entry: AuditEntry): { message: string; emailedTo: string[] } | null {
    if (entry.action !== "alert" || !entry.changes) return null;
    const message = typeof entry.changes.message === "string" ? entry.changes.message : "";
    const emailed = Array.isArray(entry.changes.emailed_to) ? entry.changes.emailed_to : [];
    return { message, emailedTo: emailed.filter((e): e is string => typeof e === "string") };
}

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/** Parse a comma/space separated list of emails; invalid ones are returned apart. */
export function parseEmailList(text: string): { emails: string[]; invalid: string[] } {
    const parts = text.split(/[\s,;]+/).map((p) => p.trim().toLowerCase()).filter(Boolean);
    const emails = Array.from(new Set(parts.filter((p) => EMAIL.test(p))));
    const invalid = parts.filter((p) => !EMAIL.test(p));
    return { emails, invalid };
}

export interface MappingPreview {
    lead_count: number;
    phone_raw?: string | null;
    phone?: string | null;
    outcome: "received" | "invalid_number" | "rejected";
    reason?: string | null;
    fields: Record<string, string | null>;
    variables: Record<string, string>;
    paths: { path: string; value: string }[];
}

export interface LeadQuery {
    endpointId?: number;
    statuses?: string[];
    search?: string;
    receivedFrom?: Date | null;
    receivedTo?: Date | null;
    limit?: number;
    offset?: number;
}

const BASE = "/api/v1/webhook-sync";

async function unwrap<T>(
    request: Promise<{ data?: T; error?: unknown }>,
    fallback: string,
): Promise<T> {
    const { data, error } = await request;
    if (error || data === undefined) {
        throw new Error(detailFromError(error, fallback));
    }
    return data;
}

export function listEndpoints(): Promise<WebhookEndpoint[]> {
    return unwrap(
        client.get<{ 200: { endpoints: WebhookEndpoint[] } }>({ url: `${BASE}/endpoints` }),
        "Failed to load webhook endpoints",
    ).then((data) => data.endpoints);
}

export function getEndpoint(id: number): Promise<WebhookEndpoint> {
    return unwrap(
        client.get<{ 200: WebhookEndpoint }>({ url: `${BASE}/endpoints/${id}` }),
        "Failed to load the webhook endpoint",
    );
}

export function createEndpoint(input: EndpointInput): Promise<WebhookEndpoint> {
    return unwrap(
        client.post<{ 200: WebhookEndpoint }>({ url: `${BASE}/endpoints`, body: input }),
        "Failed to create the webhook endpoint",
    );
}

export function updateEndpoint(id: number, changes: Partial<EndpointInput>): Promise<WebhookEndpoint> {
    return unwrap(
        client.patch<{ 200: WebhookEndpoint }>({ url: `${BASE}/endpoints/${id}`, body: changes }),
        "Failed to save the webhook endpoint",
    );
}

export function regenerateSecret(id: number): Promise<WebhookEndpoint> {
    return unwrap(
        client.post<{ 200: WebhookEndpoint }>({ url: `${BASE}/endpoints/${id}/regenerate-secret` }),
        "Failed to regenerate the secret",
    );
}

export function resumeCalling(id: number): Promise<WebhookEndpoint> {
    return unwrap(
        client.post<{ 200: WebhookEndpoint }>({ url: `${BASE}/endpoints/${id}/resume-calling` }),
        "Failed to resume calling",
    );
}

export async function deleteEndpoint(id: number): Promise<void> {
    await unwrap(
        client.delete<{ 200: { success: boolean } }>({ url: `${BASE}/endpoints/${id}` }),
        "Failed to delete the webhook endpoint",
    );
}

/** Query parameters for the leads list; empty values are left out. */
export function leadQueryParams(query: LeadQuery): Record<string, string | number> {
    const params: Record<string, string | number> = {
        limit: query.limit ?? 50,
        offset: query.offset ?? 0,
    };
    if (query.endpointId !== undefined) params.endpoint_id = query.endpointId;
    if (query.statuses && query.statuses.length > 0) params.status = query.statuses.join(",");
    if (query.search && query.search.trim()) params.search = query.search.trim();
    if (query.receivedFrom) params.received_from = query.receivedFrom.toISOString();
    if (query.receivedTo) params.received_to = query.receivedTo.toISOString();
    return params;
}

export function listLeads(query: LeadQuery): Promise<{ leads: WebhookLead[]; total: number }> {
    return unwrap(
        client.get<{ 200: { leads: WebhookLead[]; total: number } }>({
            url: `${BASE}/leads`,
            query: leadQueryParams(query),
        }),
        "Failed to load leads",
    );
}

export function getLead(id: number): Promise<WebhookLead> {
    return unwrap(client.get<{ 200: WebhookLead }>({ url: `${BASE}/leads/${id}` }), "Failed to load the lead");
}

export function getLeadStats(endpointId?: number): Promise<LeadStats> {
    return unwrap(
        client.get<{ 200: LeadStats }>({
            url: `${BASE}/leads/stats`,
            query: endpointId === undefined ? {} : { endpoint_id: endpointId },
        }),
        "Failed to load lead counts",
    );
}

export function getSyncStats(endpointId?: number, days = 30): Promise<SyncStats> {
    const query: Record<string, number> = { days };
    if (endpointId !== undefined) query.endpoint_id = endpointId;
    return unwrap(client.get<{ 200: SyncStats }>({ url: `${BASE}/stats`, query }), "Failed to load stats");
}

/** "45s", "3m", "2h 5m": how long a lead waited for its first call. */
export function formatWait(seconds?: number | null): string {
    if (seconds == null) return "—";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export const CALLBACK_STATUS_LABELS: Record<string, string> = {
    pending: "Sending",
    succeeded: "Delivered",
    dead_letter: "Failed (gave up)",
};

export interface TestLeadResult {
    /** What the endpoint answered, exactly as a CRM would see it. */
    status_code: number;
    body: Record<string, unknown>;
    lead_id?: number | null;
}

export function sendTestLead(endpointId: number, phone: string, name?: string): Promise<TestLeadResult> {
    return unwrap(
        client.post<{ 200: TestLeadResult }>({
            url: `${BASE}/endpoints/${endpointId}/test-lead`,
            body: { phone, name: name || null },
        }),
        "Could not send the test lead",
    );
}

/** Lead statuses from which "Call again" is offered (calling is over or never began). */
export const CALLABLE_AGAIN = new Set(["received", "on_hold", "skipped", "failed", "no_answer", "busy", "completed"]);

/** Lead statuses that can be skipped ("Don't call"): on hold, or stored and never queued. */
export const SKIPPABLE = new Set(["on_hold", "received"]);

export type LeadAction = "call" | "skip";

export interface BulkLeadActionResult {
    action: LeadAction;
    done: number[];
    not_done: { lead_id: number; reason: string }[];
}

/** Call or skip several chosen leads (up to 500); each is checked on its own. */
export function bulkLeadAction(leadIds: number[], action: LeadAction): Promise<BulkLeadActionResult> {
    return unwrap(
        client.post<{ 200: BulkLeadActionResult }>({
            url: `${BASE}/leads/bulk-action`,
            body: { lead_ids: leadIds, action },
        }),
        action === "call" ? "Could not call the leads" : "Could not skip the leads",
    );
}

/** Call or skip every lead an endpoint stored while it was paused. */
export function onHoldLeadsAction(endpointId: number, action: LeadAction): Promise<BulkLeadActionResult> {
    return unwrap(
        client.post<{ 200: BulkLeadActionResult }>({
            url: `${BASE}/endpoints/${endpointId}/on-hold-leads`,
            body: { action },
        }),
        action === "call" ? "Could not call the leads on hold" : "Could not skip the leads on hold",
    );
}

function plural(n: number, word: string) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** One line for a toast: what was done, and why the rest wasn't. */
export function summarizeBulkResult(result: BulkLeadActionResult): { message: string; ok: boolean } {
    const verb = result.action === "call" ? "queued for calling" : "skipped (won't be called)";
    const parts: string[] = [];
    if (result.done.length > 0) parts.push(`${plural(result.done.length, "lead")} ${verb}`);
    if (result.not_done.length > 0) {
        const reasons = Array.from(new Set(result.not_done.map((n) => n.reason)));
        parts.push(`${plural(result.not_done.length, "lead")} not changed: ${reasons.join("; ")}`);
    }
    if (parts.length === 0) parts.push("Nothing to do");
    return { message: parts.join(". "), ok: result.not_done.length === 0 };
}

/** The endpoint's Leads tab, filtered to the leads on hold. */
export function onHoldLeadsUrl(endpointId: number): string {
    const status = leadFilterAttributes.find((a) => a.id === "leadStatus")!;
    const filters = encodeFiltersToURL([
        { attribute: status, value: { codes: [LEAD_STATUS_META.on_hold.label] }, isValid: true },
    ]);
    return `/webhook-sync/${endpointId}?tab=leads&page=1&${filters}`;
}

export function callLeadAgain(leadId: number): Promise<WebhookLead> {
    return unwrap(
        client.post<{ 200: WebhookLead }>({ url: `${BASE}/leads/${leadId}/call-again` }),
        "Could not call the lead again",
    );
}

export function stopCallingLead(leadId: number): Promise<WebhookLead> {
    return unwrap(
        client.post<{ 200: WebhookLead }>({ url: `${BASE}/leads/${leadId}/stop-calling` }),
        "Could not stop calling the lead",
    );
}

export async function resendLeadResult(leadId: number): Promise<void> {
    await unwrap(
        client.post<{ 200: { success: boolean } }>({ url: `${BASE}/leads/${leadId}/resend-result` }),
        "Could not resend the result",
    );
}

/** Download the leads matching a query as CSV (up to 10,000). */
export async function downloadLeadsCsv(query: LeadQuery): Promise<void> {
    const params = leadQueryParams(query);
    delete params.limit;
    delete params.offset;
    const csv = await unwrap(
        client.get<{ 200: string }>({ url: `${BASE}/leads/export`, query: params, parseAs: "text" }),
        "Could not export the leads",
    );
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `webhook-sync-leads-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

export function listRequestLogs(
    endpointId: number,
    limit = 50,
    offset = 0,
): Promise<{ logs: RequestLog[]; total: number }> {
    return unwrap(
        client.get<{ 200: { logs: RequestLog[]; total: number } }>({
            url: `${BASE}/endpoints/${endpointId}/logs`,
            query: { limit, offset },
        }),
        "Failed to load request logs",
    );
}

export function listAudit(
    endpointId?: number,
    limit = 50,
    offset = 0,
): Promise<{ entries: AuditEntry[]; total: number }> {
    const query: Record<string, number> = { limit, offset };
    if (endpointId !== undefined) query.endpoint_id = endpointId;
    return unwrap(
        client.get<{ 200: { entries: AuditEntry[]; total: number } }>({ url: `${BASE}/audit`, query }),
        "Failed to load the change history",
    );
}

export function previewMapping(
    sample: string,
    contentType: string,
    fieldMapping: FieldMapping,
): Promise<MappingPreview> {
    return unwrap(
        client.post<{ 200: MappingPreview }>({
            url: `${BASE}/mapping-preview`,
            body: { sample, content_type: contentType, field_mapping: fieldMapping },
        }),
        "Could not read the sample",
    );
}

// ======== Display helpers ========

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

export const LEAD_STATUS_META: Record<LeadStatus, { label: string; variant: BadgeVariant; className: string }> = {
    received: { label: "Received", variant: "secondary", className: "bg-sky-500/15 text-sky-700 dark:text-sky-300" },
    on_hold: { label: "On hold", variant: "outline", className: "bg-orange-500/15 text-orange-700 dark:text-orange-300" },
    queued: { label: "Queued", variant: "secondary", className: "bg-sky-500/15 text-sky-700 dark:text-sky-300" },
    scheduled: { label: "Scheduled", variant: "secondary", className: "bg-indigo-500/15 text-indigo-700 dark:text-indigo-300" },
    calling: { label: "Calling", variant: "default", className: "bg-violet-500/15 text-violet-700 dark:text-violet-300" },
    completed: { label: "Completed", variant: "default", className: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" },
    no_answer: { label: "No answer", variant: "outline", className: "bg-amber-500/15 text-amber-700 dark:text-amber-300" },
    busy: { label: "Busy", variant: "outline", className: "bg-amber-500/15 text-amber-700 dark:text-amber-300" },
    duplicate: { label: "Duplicate", variant: "outline", className: "bg-zinc-500/15 text-zinc-700 dark:text-zinc-300" },
    skipped: { label: "Skipped", variant: "outline", className: "bg-zinc-500/15 text-zinc-700 dark:text-zinc-300" },
    invalid_number: { label: "Invalid number", variant: "destructive", className: "bg-red-500/15 text-red-700 dark:text-red-300" },
    failed: { label: "Failed", variant: "destructive", className: "bg-red-500/15 text-red-700 dark:text-red-300" },
    do_not_call: { label: "Do not call", variant: "destructive", className: "bg-red-500/15 text-red-700 dark:text-red-300" },
};

export const LEAD_STATUSES = Object.keys(LEAD_STATUS_META) as LeadStatus[];

export function leadStatusMeta(status: string) {
    return (
        LEAD_STATUS_META[status as LeadStatus] ?? {
            label: status.replace(/_/g, " "),
            variant: "outline" as BadgeVariant,
            className: "",
        }
    );
}

export const AUTH_TYPE_LABELS: Record<AuthType, { label: string; hint: string }> = {
    api_key: { label: "API key header", hint: "Your CRM sends X-API-Key: <secret>. Most CRMs support a custom header." },
    hmac: {
        label: "HMAC signature",
        hint: "Signed requests (Zapier/n8n code steps or your own code); the secret never travels.",
    },
    url_token: {
        label: "Token in URL",
        hint: "For CRMs that can't set headers. Least secure: the URL can end up in logs.",
    },
};

export const STANDARD_FIELDS: { key: StandardField; label: string; required?: boolean }[] = [
    { key: "phone", label: "Phone", required: true },
    { key: "name", label: "Name" },
    { key: "email", label: "Email" },
    { key: "external_lead_id", label: "CRM lead ID" },
    { key: "source", label: "Source" },
    { key: "city", label: "City" },
    { key: "language_preference", label: "Language" },
];

export const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Whether an endpoint's leads are being called right now, for display. */
export function callingStatus(
    endpoint: Pick<WebhookEndpoint, "is_active" | "auto_call" | "calling_state">,
): { label: string; tone: "good" | "warn" | "off"; hint?: string } {
    if (!endpoint.auto_call) return { label: "Not calling", tone: "off", hint: "Auto-call is off; leads are only stored." };
    if (!endpoint.is_active) return { label: "Paused", tone: "off", hint: "The endpoint is paused." };
    if (endpoint.calling_state === "paused") {
        return {
            label: "Calling stopped",
            tone: "warn",
            hint: "Calling was stopped automatically after too many failed calls in a row. Check the telephony setup, then resume.",
        };
    }
    if (endpoint.calling_state && endpoint.calling_state !== "running") {
        return { label: `Calling ${endpoint.calling_state}`, tone: "warn" };
    }
    return { label: "Calling", tone: "good", hint: endpoint.calling_state ? undefined : "Starts with the first lead." };
}

export const DEFAULT_CALL_SETTINGS: CallSettings = {
    delay_minutes: 0,
    calling_hours: { start: "09:00", end: "21:00", timezone: "Asia/Kolkata", days: [0, 1, 2, 3, 4, 5, 6] },
    retries: { max_attempts: 3, gap_minutes: 30, on_statuses: ["no_answer", "busy"] },
    dedupe_window_hours: 24,
    reenquiry_days: 30,
    callback_url: null,
    telephony_configuration_id: null,
    alert_emails: [],
};

/** A field mapping with blank paths removed, as the API expects. */
export function cleanMapping(mapping: FieldMapping): FieldMapping {
    const cleaned: FieldMapping = { custom: {} };
    for (const { key } of STANDARD_FIELDS) {
        const path = mapping[key]?.trim();
        if (path) cleaned[key] = path;
    }
    for (const [name, path] of Object.entries(mapping.custom ?? {})) {
        if (name.trim() && path.trim()) cleaned.custom[name.trim()] = path.trim();
    }
    if (mapping.only_mapped) cleaned.only_mapped = true;
    const valueMaps = cleanValueMaps(mapping.value_maps);
    if (Object.keys(valueMaps).length > 0) cleaned.value_maps = valueMaps;
    return cleaned;
}

/** Value translations with blank fields and incomplete rows removed. */
export function cleanValueMaps(
    maps: Record<string, Record<string, string>> | undefined,
): Record<string, Record<string, string>> {
    const cleaned: Record<string, Record<string, string>> = {};
    for (const [field, rules] of Object.entries(maps ?? {})) {
        const name = field.trim();
        if (!name) continue;
        const kept: Record<string, string> = {};
        for (const [from, to] of Object.entries(rules ?? {})) {
            if (from.trim() && to.trim()) kept[from.trim()] = to.trim();
        }
        if (Object.keys(kept).length > 0) cleaned[name] = { ...(cleaned[name] ?? {}), ...kept };
    }
    return cleaned;
}

/** A ready-to-run curl command for testing an endpoint. */
export function curlExample(endpoint: Pick<WebhookEndpoint, "webhook_url" | "auth_type" | "secret">): string {
    const body = `'{"name":"Test Lead","mobile":"9876543210","source":"Test"}'`;
    if (endpoint.auth_type === "hmac") {
        return [
            `BODY=${body}`,
            `TS=$(date +%s)`,
            `SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac '${endpoint.secret}' | sed 's/^.* //')`,
            `curl -X POST '${endpoint.webhook_url}' \\`,
            `  -H 'Content-Type: application/json' \\`,
            `  -H "X-Botrix-Timestamp: $TS" \\`,
            `  -H "X-Botrix-Signature: $SIG" \\`,
            `  -d "$BODY"`,
        ].join("\n");
    }
    const lines = [`curl -X POST '${endpoint.webhook_url}' \\`, `  -H 'Content-Type: application/json' \\`];
    if (endpoint.auth_type === "api_key") lines.push(`  -H 'X-API-Key: ${endpoint.secret}' \\`);
    lines.push(`  -d ${body}`);
    return lines.join("\n");
}

/** Hide all but the last four characters of a secret. */
export function maskSecret(secret: string): string {
    if (secret.length <= 4) return "••••";
    return `${"•".repeat(Math.min(24, secret.length - 4))}${secret.slice(-4)}`;
}

/** The webhook URL with any URL token hidden, for display. */
export function displayUrl(url: string): string {
    return url.replace(/([?&]token=)[^&]+/, "$1••••");
}

export const AUDIT_ACTION_LABELS: Record<string, string> = {
    created: "Created",
    updated: "Changed",
    paused: "Paused",
    resumed: "Resumed",
    secret_regenerated: "Secret regenerated",
    calling_resumed: "Calling resumed",
    deleted: "Deleted",
    alert: "Alert",
    on_hold_called: "Called leads on hold",
    on_hold_skipped: "Skipped leads on hold",
};

export const AUDIT_FIELD_LABELS: Record<string, string> = {
    leads: "Leads (on hold → done)",
    name: "Name",
    workflow_id: "Agent",
    auth_type: "Authentication",
    auto_call: "Auto-call",
    field_mapping: "Field mapping",
    call_settings: "Call settings",
    rate_limit_per_minute: "Rate limit",
};

export interface TimelineStep {
    title: string;
    detail?: string | null;
    at?: string | null;
    tone: "info" | "good" | "bad" | "pending";
}

const CALL_RESULT_LABELS: Record<string, string> = {
    completed: "Call connected",
    no_answer: "No answer",
    busy: "Line busy",
    failed: "Call failed",
};

/** What happened to a lead so far, oldest first. */
export function leadTimeline(lead: WebhookLead): TimelineStep[] {
    const steps: TimelineStep[] = [
        { title: "Received from the CRM", detail: lead.source ? `Source: ${lead.source}` : null, at: lead.received_at, tone: "info" },
    ];
    if (lead.status === "invalid_number") {
        steps.push({ title: "Not called: invalid number", detail: lead.status_reason, tone: "bad" });
        return steps;
    }
    if (lead.status === "duplicate") {
        steps.push({
            title: "Not called: duplicate",
            detail: lead.duplicate_of_lead_id
                ? `Same number as lead #${lead.duplicate_of_lead_id}`
                : lead.status_reason,
            tone: "bad",
        });
        return steps;
    }
    if (lead.status === "do_not_call") {
        steps.push({ title: "Not called: on the do-not-call list", detail: lead.status_reason, tone: "bad" });
        return steps;
    }
    if (lead.status === "on_hold") {
        steps.push({
            title: "On hold: waiting for you to choose",
            detail: `${lead.status_reason ?? "Received while the endpoint was paused"}. Call it, or choose not to.`,
            tone: "pending",
        });
        return steps;
    }
    if (lead.status === "skipped") {
        steps.push({ title: "Not called: skipped", detail: lead.status_reason, tone: "bad" });
        return steps;
    }
    if (lead.call_attempts > 0) {
        const result = lead.last_call_status ? CALL_RESULT_LABELS[lead.last_call_status] ?? lead.last_call_status : null;
        steps.push({
            title: `Called ${lead.call_attempts} time${lead.call_attempts === 1 ? "" : "s"}`,
            detail: [result && `Last result: ${result}`, lead.disposition && `Disposition: ${lead.disposition}`]
                .filter(Boolean)
                .join(" · ") || null,
            at: lead.updated_at,
            tone: lead.status === "completed" ? "good" : lead.status === "failed" ? "bad" : "info",
        });
    }
    if (lead.status === "failed" && !lead.next_retry_at) {
        // Why it ended: no balance, the agent or telephony setup missing, the
        // provider refusing the call, ... (the backend records the reason).
        steps.push({
            title: lead.call_attempts > 0 ? "Calling stopped: the call failed" : "Not called: the call could not be placed",
            detail: lead.status_reason,
            tone: "bad",
        });
        return steps;
    }
    if (lead.status === "calling") {
        steps.push({ title: "Call in progress", tone: "pending" });
    } else if (lead.next_retry_at && ["no_answer", "busy", "failed", "scheduled"].includes(lead.status)) {
        steps.push({
            title: lead.call_attempts > 0 ? "Next attempt scheduled" : "First call scheduled",
            detail: lead.status_reason,
            at: lead.next_retry_at,
            tone: "pending",
        });
    } else if (["received", "queued", "scheduled"].includes(lead.status)) {
        steps.push({ title: "Waiting to be called", detail: lead.status_reason, tone: "pending" });
    } else if (lead.status === "completed") {
        steps.push({ title: "Done", tone: "good" });
    }
    return steps;
}

// ======== Filters (the same FilterBuilder the Agent Runs page uses) ========

export const leadFilterAttributes: FilterAttribute[] = [
    {
        id: "dateRange",
        type: "dateRange",
        label: "Received",
        config: { maxRangeDays: 90, datePresets: ["today", "yesterday", "last7days", "last30days"] },
    },
    {
        id: "leadStatus",
        type: "multiSelect",
        label: "Status",
        config: {
            options: LEAD_STATUSES.map((s) => LEAD_STATUS_META[s].label),
            searchable: false,
            maxSelections: LEAD_STATUSES.length,
            showSelectAll: true,
        },
    },
    {
        id: "leadSearch",
        type: "text",
        label: "Name, phone or email",
        config: { placeholder: "Search leads (partial match)", maxLength: 100 },
    },
];

/** Turn FilterBuilder filters into a leads query. */
export function leadQueryFromFilters(filters: ActiveFilter[]): Pick<LeadQuery, "statuses" | "search" | "receivedFrom" | "receivedTo"> {
    const query: Pick<LeadQuery, "statuses" | "search" | "receivedFrom" | "receivedTo"> = {};
    const byLabel = Object.fromEntries(LEAD_STATUSES.map((s) => [LEAD_STATUS_META[s].label, s]));
    for (const filter of filters) {
        if (filter.attribute.id === "dateRange") {
            const value = filter.value as DateRangeValue;
            query.receivedFrom = value.from ? new Date(value.from) : null;
            query.receivedTo = value.to ? new Date(value.to) : null;
        } else if (filter.attribute.id === "leadStatus") {
            const codes = (filter.value as MultiSelectValue).codes ?? [];
            query.statuses = codes.map((c) => byLabel[c] ?? c);
        } else if (filter.attribute.id === "leadSearch") {
            query.search = (filter.value as TextValue).value;
        }
    }
    return query;
}
