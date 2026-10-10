import { describe, expect, it } from "vitest";

import type { ActiveFilter } from "@/types/filters";

import {
    alertDetails,
    callingStatus,
    cleanMapping,
    curlExample,
    displayUrl,
    formatWait,
    leadFilterAttributes,
    leadQueryFromFilters,
    leadQueryParams,
    leadStatusMeta,
    leadTimeline,
    maskSecret,
    onHoldLeadsUrl,
    parseEmailList,
    summarizeBulkResult,
    type WebhookLead,
} from "./webhookSync";

const attr = (id: string) => leadFilterAttributes.find((a) => a.id === id)!;

function lead(overrides: Partial<WebhookLead> = {}): WebhookLead {
    return {
        id: 1,
        endpoint_id: 2,
        variables: {},
        status: "received",
        call_attempts: 0,
        received_at: "2026-09-28T10:00:00Z",
        updated_at: "2026-09-28T10:00:00Z",
        phone_masked: false,
        ...overrides,
    };
}

describe("leadQueryParams", () => {
    it("sends only what is set, with statuses comma-joined", () => {
        expect(leadQueryParams({})).toEqual({ limit: 50, offset: 0 });
        const from = new Date("2026-09-01T00:00:00Z");
        expect(
            leadQueryParams({
                endpointId: 3,
                statuses: ["duplicate", "invalid_number"],
                search: "  rahul ",
                receivedFrom: from,
                receivedTo: null,
                limit: 10,
                offset: 20,
            }),
        ).toEqual({
            limit: 10,
            offset: 20,
            endpoint_id: 3,
            status: "duplicate,invalid_number",
            search: "rahul",
            received_from: from.toISOString(),
        });
        expect(leadQueryParams({ statuses: [], search: "   " })).toEqual({ limit: 50, offset: 0 });
    });
});

describe("leadQueryFromFilters", () => {
    it("maps the FilterBuilder filters onto the API query", () => {
        const filters: ActiveFilter[] = [
            { attribute: attr("leadStatus"), value: { codes: ["Invalid number", "Duplicate"] }, isValid: true },
            { attribute: attr("leadSearch"), value: { value: "9876" }, isValid: true },
            {
                attribute: attr("dateRange"),
                value: { from: new Date("2026-09-01T00:00:00Z"), to: new Date("2026-09-02T00:00:00Z") },
                isValid: true,
            },
        ];
        const query = leadQueryFromFilters(filters);
        expect(query.statuses).toEqual(["invalid_number", "duplicate"]);
        expect(query.search).toBe("9876");
        expect(query.receivedFrom?.toISOString()).toBe("2026-09-01T00:00:00.000Z");
        expect(query.receivedTo?.toISOString()).toBe("2026-09-02T00:00:00.000Z");
    });
});

describe("cleanMapping", () => {
    it("drops blank paths and incomplete custom variables, trimming the rest", () => {
        expect(
            cleanMapping({
                phone: " Current.Phone ",
                name: "",
                email: "   ",
                custom: { " product ": " data.product ", empty: "", "": "x" },
            }),
        ).toEqual({ phone: "Current.Phone", custom: { product: "data.product" } });
    });
});

describe("cleanMapping only_mapped", () => {
    it("keeps the switch only when it is on", () => {
        expect(cleanMapping({ phone: "Phone", custom: {}, only_mapped: true })).toEqual({
            phone: "Phone",
            custom: {},
            only_mapped: true,
        });
        expect(cleanMapping({ phone: "Phone", custom: {}, only_mapped: false })).toEqual({ phone: "Phone", custom: {} });
    });
});

describe("cleanMapping value_maps", () => {
    it("trims, drops incomplete rows and empty tables", () => {
        expect(
            cleanMapping({
                custom: {},
                value_maps: {
                    " source ": { " Contact us ": " Website ", "FB Leads ad": "", "": "x" },
                    city: { "": "" },
                    "  ": { a: "b" },
                },
            }),
        ).toEqual({ custom: {}, value_maps: { source: { "Contact us": "Website" } } });
        expect(cleanMapping({ custom: {}, value_maps: {} })).toEqual({ custom: {} });
    });
});

describe("curlExample", () => {
    const url = "https://voice-app.example/api/v1/webhooks/inbound/abc";

    it("sends the API key header", () => {
        const cmd = curlExample({ webhook_url: url, auth_type: "api_key", secret: "s3cret" });
        expect(cmd).toContain(`curl -X POST '${url}'`);
        expect(cmd).toContain("-H 'X-API-Key: s3cret'");
    });

    it("signs '<timestamp>.<body>' for HMAC", () => {
        const cmd = curlExample({ webhook_url: url, auth_type: "hmac", secret: "s3cret" });
        expect(cmd).toContain(`printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac 's3cret'`);
        expect(cmd).toContain('X-Botrix-Timestamp: $TS');
        expect(cmd).toContain('X-Botrix-Signature: $SIG');
        expect(cmd).not.toContain("X-API-Key");
    });

    it("needs no header for a URL token", () => {
        const cmd = curlExample({ webhook_url: `${url}?token=t`, auth_type: "url_token", secret: "t" });
        expect(cmd).not.toContain("X-API-Key");
        expect(cmd).toContain("?token=t");
    });
});

describe("secret display", () => {
    it("keeps only the last four characters", () => {
        expect(maskSecret("abcdefgh1234")).toBe("••••••••1234");
        expect(maskSecret("abc")).toBe("••••");
    });

    it("hides a token in the URL", () => {
        expect(displayUrl("https://x/in/abc?token=secret&x=1")).toBe("https://x/in/abc?token=••••&x=1");
        expect(displayUrl("https://x/in/abc")).toBe("https://x/in/abc");
    });
});

describe("leadStatusMeta", () => {
    it("labels known statuses and falls back for unknown ones", () => {
        expect(leadStatusMeta("invalid_number").label).toBe("Invalid number");
        expect(leadStatusMeta("some_new_state").label).toBe("some new state");
    });
});

describe("leadTimeline", () => {
    it("explains why a duplicate isn't called", () => {
        const steps = leadTimeline(lead({ status: "duplicate", duplicate_of_lead_id: 7 }));
        expect(steps.map((s) => s.title)).toEqual(["Received from the CRM", "Not called: duplicate"]);
        expect(steps[1].detail).toBe("Same number as lead #7");
        expect(steps[1].tone).toBe("bad");
    });

    it("shows a new lead waiting to be called", () => {
        expect(leadTimeline(lead()).map((s) => s.title)).toEqual(["Received from the CRM", "Waiting to be called"]);
    });

    it("shows attempts, the last result and the next retry", () => {
        const steps = leadTimeline(
            lead({
                status: "no_answer",
                call_attempts: 2,
                last_call_status: "no_answer",
                next_retry_at: "2026-09-28T11:00:00Z",
            }),
        );
        expect(steps.map((s) => s.title)).toEqual([
            "Received from the CRM",
            "Called 2 times",
            "Next attempt scheduled",
        ]);
        expect(steps[1].detail).toBe("Last result: No answer");
    });

    it("says why a lead could not be called", () => {
        const steps = leadTimeline(lead({ status: "failed", status_reason: "Insufficient wallet balance" }));
        expect(steps.map((s) => s.title)).toEqual([
            "Received from the CRM",
            "Not called: the call could not be placed",
        ]);
        expect(steps[1]).toMatchObject({ detail: "Insufficient wallet balance", tone: "bad" });
    });

    it("says why calling stopped after a failed call", () => {
        const steps = leadTimeline(
            lead({ status: "failed", call_attempts: 1, last_call_status: "failed", status_reason: "Carrier rejected the call" }),
        );
        expect(steps.at(-1)).toMatchObject({
            title: "Calling stopped: the call failed",
            detail: "Carrier rejected the call",
        });
    });

    it("calls a first call waiting for calling hours a first call, not a next attempt", () => {
        const steps = leadTimeline(
            lead({ status: "scheduled", next_retry_at: "2026-09-29T03:30:00Z", status_reason: "Waiting for calling hours" }),
        );
        expect(steps.at(-1)).toMatchObject({ title: "First call scheduled", detail: "Waiting for calling hours" });
    });

    it("marks a completed lead done", () => {
        const steps = leadTimeline(
            lead({ status: "completed", call_attempts: 1, last_call_status: "completed", disposition: "Interested" }),
        );
        expect(steps.at(-1)).toMatchObject({ title: "Done", tone: "good" });
        expect(steps[1].detail).toBe("Last result: Call connected · Disposition: Interested");
    });
});

describe("callingStatus", () => {
    it("says when leads are and aren't being called", () => {
        expect(callingStatus({ is_active: true, auto_call: true, calling_state: "running" })).toMatchObject({
            label: "Calling",
            tone: "good",
        });
        expect(callingStatus({ is_active: true, auto_call: true, calling_state: null }).hint).toBe(
            "Starts with the first lead.",
        );
        expect(callingStatus({ is_active: true, auto_call: false, calling_state: "running" }).label).toBe(
            "Not calling",
        );
        expect(callingStatus({ is_active: false, auto_call: true, calling_state: "paused" }).label).toBe("Paused");
    });

    it("flags calling stopped by the circuit breaker while the endpoint is active", () => {
        const status = callingStatus({ is_active: true, auto_call: true, calling_state: "paused" });
        expect(status).toMatchObject({ label: "Calling stopped", tone: "warn" });
        expect(status.hint).toContain("too many failed calls");
    });
});

describe("formatWait", () => {
    it("shows a wait in the largest sensible unit", () => {
        expect(formatWait(null)).toBe("—");
        expect(formatWait(42.4)).toBe("42s");
        expect(formatWait(180)).toBe("3m");
        expect(formatWait(3600)).toBe("1h");
        expect(formatWait(7500)).toBe("2h 5m");
    });
});

describe("parseEmailList", () => {
    it("splits on commas, spaces and semicolons, lower-cases and dedupes", () => {
        expect(parseEmailList(" Ops@Client.com, ops@client.com;  m@x.co ")).toEqual({
            emails: ["ops@client.com", "m@x.co"],
            invalid: [],
        });
    });
    it("reports what isn't an email, and keeps a half-typed list usable", () => {
        expect(parseEmailList("a@b.co, nope, c@d")).toEqual({ emails: ["a@b.co"], invalid: ["nope", "c@d"] });
        expect(parseEmailList("a@b.co,")).toEqual({ emails: ["a@b.co"], invalid: [] });
        expect(parseEmailList("")).toEqual({ emails: [], invalid: [] });
    });
});

describe("alertDetails", () => {
    const base = { id: 1, created_at: "2026-10-02T10:00:00Z" };
    it("reads an alert entry", () => {
        expect(
            alertDetails({
                ...base,
                action: "alert",
                changes: { kind: "calls_not_placed", message: "Wallet empty", emailed_to: ["a@b.co"] },
            }),
        ).toEqual({ message: "Wallet empty", emailedTo: ["a@b.co"] });
    });
    it("is null for other entries and safe on odd shapes", () => {
        expect(alertDetails({ ...base, action: "updated", changes: {} })).toBeNull();
        expect(alertDetails({ ...base, action: "alert", changes: { message: 5, emailed_to: "x" } })).toEqual({
            message: "",
            emailedTo: [],
        });
    });
});

describe("choosing leads", () => {
    it("summarizes a bulk result for a toast", () => {
        expect(summarizeBulkResult({ action: "call", done: [1], not_done: [] })).toEqual({
            message: "1 lead queued for calling",
            ok: true,
        });
        const mixed = summarizeBulkResult({
            action: "call",
            done: [1, 2],
            not_done: [
                { lead_id: 3, reason: "This number opted out of calls" },
                { lead_id: 4, reason: "This number opted out of calls" },
                { lead_id: 5, reason: "Resume the endpoint first" },
            ],
        });
        expect(mixed).toEqual({
            message:
                "2 leads queued for calling. 3 leads not changed: This number opted out of calls; Resume the endpoint first",
            ok: false,
        });
        expect(summarizeBulkResult({ action: "skip", done: [], not_done: [] }).message).toBe("Nothing to do");
    });

    it("links to the endpoint's leads on hold", () => {
        const url = onHoldLeadsUrl(7);
        expect(url.startsWith("/webhook-sync/7?tab=leads&page=1&")).toBe(true);
        const filters = JSON.parse(new URLSearchParams(url.split("?")[1]).get("filters")!);
        expect(filters).toEqual([{ id: "leadStatus", value: { codes: ["On hold"] } }]);
    });

    it("explains an on-hold or skipped lead in its timeline", () => {
        const base = {
            id: 1,
            endpoint_id: 3,
            variables: {},
            call_attempts: 0,
            received_at: "2026-10-02T05:00:00Z",
            updated_at: "2026-10-02T05:00:00Z",
            phone_masked: false,
        };
        const held = leadTimeline({ ...base, status: "on_hold", status_reason: "Received while the endpoint was paused" });
        expect(held.at(-1)).toMatchObject({ title: "On hold: waiting for you to choose", tone: "pending" });
        const skipped = leadTimeline({ ...base, status: "skipped", status_reason: "Not called (chosen on the dashboard)" });
        expect(skipped.at(-1)).toMatchObject({ title: "Not called: skipped", tone: "bad" });
    });
});
