import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuditEntry, LeadStats, RequestLog, SyncStats, WebhookEndpoint, WebhookLead } from "@/lib/webhookSync";

// ---- Mocks: auth, routing, timezone, generated SDK, and the API wrappers ----

const push = vi.fn();
let searchParams = new URLSearchParams();
let routeParams: Record<string, string> = {};
vi.mock("next/navigation", () => ({
    useRouter: () => ({ push, back: vi.fn() }),
    usePathname: () => "/webhook-sync",
    useSearchParams: () => searchParams,
    useParams: () => routeParams,
}));
vi.mock("@/lib/auth", () => ({
    useAuth: () => ({ isAuthenticated: true, loading: false, user: { id: 1 }, redirectToLogin: vi.fn() }),
}));
vi.mock("@/lib/useOrganizationTimezone", () => ({ useOrganizationTimezone: () => "Asia/Kolkata" }));

const sdk = vi.hoisted(() => ({
    getWorkflowsSummaryApiV1WorkflowSummaryGet: vi.fn(),
    listTelephonyConfigurationsApiV1OrganizationsTelephonyConfigsGet: vi.fn(),
}));
vi.mock("@/client/sdk.gen", () => sdk);

const api = vi.hoisted(() => ({
    listLeads: vi.fn(),
    getLeadStats: vi.fn(),
    getSyncStats: vi.fn(),
    previewMapping: vi.fn(),
    listRequestLogs: vi.fn(),
    listAudit: vi.fn(),
    regenerateSecret: vi.fn(),
    getEndpoint: vi.fn(),
    getLead: vi.fn(),
    updateEndpoint: vi.fn(),
    resumeCalling: vi.fn(),
    deleteEndpoint: vi.fn(),
    bulkLeadAction: vi.fn(),
    onHoldLeadsAction: vi.fn(),
}));
vi.mock("@/lib/webhookSync", async (importOriginal) => ({
    ...(await importOriginal<typeof import("@/lib/webhookSync")>()),
    ...api,
}));

const toastMock = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastMock }));

// ---- Fixtures ----

const endpoint: WebhookEndpoint = {
    id: 3,
    name: "LeadSquared",
    endpoint_uuid: "uuid-3",
    webhook_url: "https://voice-app.example/api/v1/webhooks/inbound/uuid-3",
    auth_type: "api_key",
    secret: "s3cretvalue1234567890abcd",
    workflow_id: 7,
    workflow_name: "Think Gas agent",
    campaign_id: 40,
    calling_state: "running",
    is_active: true,
    auto_call: true,
    field_mapping: { custom: {} },
    call_settings: {
        delay_minutes: 0,
        calling_hours: { start: "09:00", end: "21:00", timezone: "Asia/Kolkata", days: [0, 1, 2, 3, 4, 5, 6] },
        retries: { max_attempts: 3, gap_minutes: 30, on_statuses: ["no_answer", "busy"] },
        dedupe_window_hours: 24,
        callback_url: null,
        telephony_configuration_id: null,
    },
    rate_limit_per_minute: 100,
    leads_today: 2,
    leads_total: 9,
    created_at: "2026-09-28T05:00:00Z",
    updated_at: "2026-09-28T05:00:00Z",
};

function lead(overrides: Partial<WebhookLead> = {}): WebhookLead {
    return {
        id: 50,
        endpoint_id: 3,
        name: "Rahul Kumar",
        phone: "+919876543210",
        phone_raw: "98765 43210",
        source: "Facebook Ads",
        variables: { city: "Patna" },
        status: "completed",
        call_attempts: 1,
        last_workflow_run_id: 9682,
        received_at: "2026-09-28T05:10:00Z",
        updated_at: "2026-09-28T05:12:00Z",
        phone_masked: false,
        ...overrides,
    };
}

const stats: LeadStats = { total: 9, today: 2, by_status: { completed: 1, duplicate: 2, invalid_number: 1 } };
const sync: SyncStats = {
    days: 30,
    leads: 9,
    callable: 6,
    called: 4,
    connected: 2,
    connect_rate: 0.5,
    median_seconds_to_first_call: 180,
    requests: 12,
    request_errors: 1,
    callbacks: { succeeded: 1, dead_letter: 1 },
};

// The first import of the component tree (Radix, the filter builder, the
// pages) is slow on Windows under a full parallel run; load it once up front
// so no single test pays for it against its timeout.
beforeAll(async () => {
    await Promise.all([
        import("./LeadsPanel"),
        import("./MappingEditor"),
        import("./EndpointForm"),
        import("./SetupPanel"),
        import("./RequestLogsPanel"),
        import("./AuditPanel"),
        import("@/app/webhook-sync/[endpointId]/page"),
        import("@/app/webhook-sync/leads/[leadId]/page"),
    ]);
}, 180000);

beforeEach(() => {
    vi.clearAllMocks();
    searchParams = new URLSearchParams();
    routeParams = {};
    sdk.getWorkflowsSummaryApiV1WorkflowSummaryGet.mockResolvedValue({ data: [{ id: 7, name: "Think Gas agent" }] });
    sdk.listTelephonyConfigurationsApiV1OrganizationsTelephonyConfigsGet.mockResolvedValue({
        data: { configurations: [{ id: 5, name: "TATA SF", provider: "tata_smartflo", phone_number_count: 2 }] },
    });
});

// ---- Leads dashboard ----

describe("LeadsPanel", { timeout: 30000 }, () => {
    async function renderPanel(leads: WebhookLead[], props = {}) {
        api.listLeads.mockResolvedValue({ leads, total: leads.length });
        api.getLeadStats.mockResolvedValue(stats);
        api.getSyncStats.mockResolvedValue(sync);
        const { LeadsPanel } = await import("./LeadsPanel");
        render(<LeadsPanel endpointNames={{ 3: "LeadSquared" }} endpointWorkflows={{ 3: 7 }} {...props} />);
        await screen.findByText(`Showing ${leads.length} of ${leads.length} leads`);
    }

    it("shows the stats cards and one row per lead", async () => {
        await renderPanel([lead(), lead({ id: 51, name: "Asha", status: "duplicate", call_attempts: 0, last_workflow_run_id: null })]);
        await waitFor(() => expect(screen.getByText("50%")).toBeTruthy());
        expect(screen.getByText("3m")).toBeTruthy(); // median time to first call
        expect(screen.getByText("of 6 callable, last 30 days")).toBeTruthy();
        expect(screen.getByText("2 duplicates, 1 invalid")).toBeTruthy();
        expect(screen.getByText(/1 failed CRM callbacks/)).toBeTruthy();

        const row = screen.getByText("Rahul Kumar").closest("tr")!;
        expect(within(row).getByText("+919876543210")).toBeTruthy();
        expect(within(row).getByText("Patna")).toBeTruthy();
        expect(within(row).getByText("Completed")).toBeTruthy();
        expect(within(row).getByText("LeadSquared")).toBeTruthy();
        expect(within(screen.getByText("Asha").closest("tr")!).getByText("Duplicate")).toBeTruthy();
    });

    it("opens a lead on click, and its call in a new tab", async () => {
        const open = vi.spyOn(window, "open").mockImplementation(() => null);
        await renderPanel([lead()]);
        fireEvent.click(screen.getByRole("button", { name: /#9682/ }));
        expect(open).toHaveBeenCalledWith("/workflow/7/run/9682", "_blank");
        expect(push).not.toHaveBeenCalled();
        fireEvent.click(screen.getByText("Rahul Kumar"));
        expect(push).toHaveBeenCalledWith("/webhook-sync/leads/50");
    });

    it("asks for the endpoint's leads only, and hides the endpoint column", async () => {
        await renderPanel([lead()], { endpointId: 3 });
        expect(api.listLeads).toHaveBeenCalledWith(expect.objectContaining({ endpointId: 3, limit: 50, offset: 0 }));
        expect(api.getSyncStats).toHaveBeenCalledWith(3);
        expect(screen.queryByText("Endpoint")).toBeNull();
    });

    it("reads filters and the page from the URL", async () => {
        searchParams = new URLSearchParams({
            page: "2",
            filters: JSON.stringify([{ id: "leadStatus", value: { codes: ["Duplicate"] } }]),
        });
        await renderPanel([lead()]);
        expect(api.listLeads).toHaveBeenCalledWith(expect.objectContaining({ statuses: ["duplicate"], offset: 50 }));
    });

    it("says there are no leads yet, and shows errors", async () => {
        await renderPanel([]).catch(() => undefined);
        expect(await screen.findByText(/No leads yet/)).toBeTruthy();
    });

    it("shows a load error", async () => {
        api.listLeads.mockRejectedValue(new Error("Failed to load leads"));
        api.getLeadStats.mockResolvedValue(stats);
        api.getSyncStats.mockResolvedValue(sync);
        const { LeadsPanel } = await import("./LeadsPanel");
        render(<LeadsPanel />);
        expect(await screen.findByText("Failed to load leads")).toBeTruthy();
    });
});

// ---- Field mapping ----

describe("LeadsPanel choosing leads", { timeout: 30000 }, () => {
    async function renderPanel(leads: WebhookLead[]) {
        api.listLeads.mockResolvedValue({ leads, total: leads.length });
        api.getLeadStats.mockResolvedValue(stats);
        api.getSyncStats.mockResolvedValue(sync);
        const onLeadsChanged = vi.fn();
        const { LeadsPanel } = await import("./LeadsPanel");
        render(<LeadsPanel endpointId={3} onLeadsChanged={onLeadsChanged} />);
        await screen.findByText(`Showing ${leads.length} of ${leads.length} leads`);
        return { onLeadsChanged };
    }

    const held = [
        lead({ id: 60, name: "Asha", status: "on_hold", call_attempts: 0, last_workflow_run_id: null }),
        lead({ id: 61, name: "Ravi", status: "on_hold", call_attempts: 0, last_workflow_run_id: null }),
        lead({ id: 62, name: "Busy Bee", status: "calling" }),
    ];

    it("ticking a row doesn't open the lead, and shows what can be done", async () => {
        await renderPanel(held);
        fireEvent.click(screen.getByLabelText("Select lead #60"));
        expect(push).not.toHaveBeenCalled();
        expect(screen.getByText("1 selected")).toBeTruthy();
        expect(screen.getByRole("button", { name: /Call selected/ })).toBeTruthy();
        fireEvent.click(screen.getByLabelText("Select lead #62")); // calling: neither callable nor skippable
        expect(screen.getByText("2 selected")).toBeTruthy();
        expect(screen.getByRole("button", { name: "Call selected (1)" })).toBeTruthy();
        expect(screen.getByRole("button", { name: "Don't call (1)" })).toBeTruthy();
        fireEvent.click(screen.getByRole("button", { name: /Clear/ }));
        expect(screen.queryByText(/selected/)).toBeNull();
    });

    it("calls only the callable selected leads after confirming", async () => {
        api.bulkLeadAction.mockResolvedValue({ action: "call", done: [60, 61], not_done: [] });
        const { onLeadsChanged } = await renderPanel(held);
        fireEvent.click(screen.getByLabelText("Select all leads on this page"));
        expect(screen.getByText("3 selected")).toBeTruthy();
        fireEvent.click(screen.getByRole("button", { name: "Call selected (2)" }));
        expect(await screen.findByText("Call 2 leads?")).toBeTruthy();
        expect(screen.getByText(/1 of the selected leads can't be called now/)).toBeTruthy();
        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: "Call" }));
        });
        expect(api.bulkLeadAction).toHaveBeenCalledWith([60, 61], "call");
        await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith("2 leads queued for calling"));
        expect(onLeadsChanged).toHaveBeenCalled();
        expect(api.listLeads).toHaveBeenCalledTimes(2); // reloaded
    });

    it("skips without a prompt, and reports leads it couldn't change", async () => {
        api.bulkLeadAction.mockResolvedValue({
            action: "skip",
            done: [60],
            not_done: [{ lead_id: 61, reason: "Only a lead on hold, or not yet queued, can be skipped" }],
        });
        await renderPanel(held);
        fireEvent.click(screen.getByLabelText("Select lead #60"));
        fireEvent.click(screen.getByLabelText("Select lead #61"));
        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: /Don't call/ }));
        });
        expect(api.bulkLeadAction).toHaveBeenCalledWith([60, 61], "skip");
        await waitFor(() =>
            expect(toastMock.warning).toHaveBeenCalledWith(
                "1 lead skipped (won't be called). 1 lead not changed: Only a lead on hold, or not yet queued, can be skipped",
            ),
        );
    });

    it("shows on-hold and skipped leads with their own status", async () => {
        await renderPanel([held[0], lead({ id: 63, name: "Meena", status: "skipped" })]);
        expect(within(screen.getByText("Asha").closest("tr")!).getByText("On hold")).toBeTruthy();
        expect(within(screen.getByText("Meena").closest("tr")!).getByText("Skipped")).toBeTruthy();
    });
});

describe("MappingEditor", () => {
    it("previews a pasted sample with the receiver's logic and shows what each field found", async () => {
        api.previewMapping.mockResolvedValue({
            lead_count: 1,
            phone_raw: "+91-9876543210",
            phone: "+919876543210",
            outcome: "received",
            fields: { name: "Rahul Kumar", email: null, external_lead_id: "P-1", source: null, city: "Patna", language_preference: null },
            variables: { name: "Rahul Kumar", city: "Patna" },
            paths: [{ path: "Current.Phone", value: "+91-9876543210" }],
        });
        const onChange = vi.fn();
        const { MappingEditor } = await import("./MappingEditor");
        render(<MappingEditor value={{ phone: " Current.Phone ", custom: {} }} onChange={onChange} />);

        fireEvent.change(screen.getByPlaceholderText(/FirstName/), { target: { value: '{"Current":{"Phone":"+91-9876543210"}}' } });
        await waitFor(() => expect(api.previewMapping).toHaveBeenCalled());
        const [sample, type, mapping] = api.previewMapping.mock.calls.at(-1)!;
        expect(sample).toContain("Current");
        expect(type).toBe("application/json");
        // Sent cleaned: trimmed paths, blanks dropped.
        expect(mapping).toEqual({ phone: "Current.Phone", custom: {} });

        expect(await screen.findByText("→ +919876543210")).toBeTruthy();
        expect(screen.getByText("→ Rahul Kumar")).toBeTruthy();
        expect(screen.getAllByText("→ not found").length).toBeGreaterThan(0);
        expect(screen.getByText("Received")).toBeTruthy();
        expect(screen.getByText("{{city}} = Patna")).toBeTruthy();
    });

    it("explains a sample the receiver would reject", async () => {
        api.previewMapping.mockRejectedValue(new Error("Request body is not valid JSON"));
        const { MappingEditor } = await import("./MappingEditor");
        render(<MappingEditor value={{ custom: {} }} onChange={vi.fn()} />);
        fireEvent.change(screen.getByPlaceholderText(/FirstName/), { target: { value: "{broken" } });
        expect(await screen.findByText("Request body is not valid JSON")).toBeTruthy();
    });

    it("maps a field by typing a path and adds a custom variable", async () => {
        const onChange = vi.fn();
        const { MappingEditor } = await import("./MappingEditor");
        render(<MappingEditor value={{ custom: {} }} onChange={onChange} />);
        fireEvent.change(screen.getAllByPlaceholderText("Auto-detect")[0], { target: { value: "data.mobile" } });
        expect(onChange).toHaveBeenLastCalledWith({ custom: {}, phone: "data.mobile" });
        fireEvent.click(screen.getByText("Add variable"));
        expect(onChange).toHaveBeenLastCalledWith({ custom: { field_1: "" } });
    });

    it("turns on 'Only keep mapped fields' and previews with it", async () => {
        api.previewMapping.mockResolvedValue({ lead_count: 1, outcome: "received", fields: {}, variables: {}, paths: [] });
        const onChange = vi.fn();
        const { MappingEditor } = await import("./MappingEditor");
        const { rerender } = render(<MappingEditor value={{ phone: "Phone", custom: {} }} onChange={onChange} />);

        const toggle = screen.getByRole("switch", { name: "Only keep mapped fields" });
        expect(toggle.getAttribute("aria-checked")).toBe("false");
        fireEvent.click(toggle);
        expect(onChange).toHaveBeenLastCalledWith({ phone: "Phone", custom: {}, only_mapped: true });

        rerender(<MappingEditor value={{ phone: "Phone", custom: {}, only_mapped: true }} onChange={onChange} />);
        expect(screen.getByText(/everything else the CRM sends is left out/)).toBeTruthy();
        fireEvent.change(screen.getByPlaceholderText(/FirstName/), { target: { value: '{"Phone":"+91-9876543210"}' } });
        await waitFor(() => expect(api.previewMapping).toHaveBeenCalled());
        expect(api.previewMapping.mock.calls.at(-1)![2]).toEqual({ phone: "Phone", custom: {}, only_mapped: true });
    });

    it("pulls in the last request's body, skipping rejected ones", async () => {
        const logs: RequestLog[] = [
            { id: 2, endpoint_id: 3, method: "POST", headers: {}, raw_body: '{"x":1}', body_truncated: false, response_code: 401, received_at: "" },
            { id: 1, endpoint_id: 3, method: "POST", headers: {}, raw_body: '{"mobile":"9876543210"}', body_truncated: false, response_code: 200, content_type: "application/json", received_at: "" },
        ];
        api.listRequestLogs.mockResolvedValue({ logs, total: 2 });
        api.previewMapping.mockResolvedValue({ lead_count: 1, outcome: "received", fields: {}, variables: {}, paths: [] });
        const { MappingEditor } = await import("./MappingEditor");
        render(<MappingEditor value={{ custom: {} }} onChange={vi.fn()} endpointId={3} />);
        fireEvent.click(screen.getByText("Use last request"));
        await waitFor(() =>
            expect((screen.getByPlaceholderText(/FirstName/) as HTMLTextAreaElement).value).toContain('"mobile": "9876543210"'),
        );
    });
});

// ---- Endpoint form ----

describe("EndpointForm", () => {
    async function renderForm(overrides = {}) {
        const { DEFAULT_CALL_SETTINGS } = await import("@/lib/webhookSync");
        const value = {
            name: "",
            workflow_id: 0,
            auth_type: "api_key" as const,
            is_active: true,
            auto_call: true,
            field_mapping: { custom: {} },
            call_settings: DEFAULT_CALL_SETTINGS,
            rate_limit_per_minute: 100,
            ...overrides,
        };
        const onChange = vi.fn();
        const { EndpointForm } = await import("./EndpointForm");
        render(<EndpointForm value={value} onChange={onChange} />);
        return { onChange, value };
    }

    it("lists problems the API would reject", async () => {
        const { endpointFormErrors } = await import("./EndpointForm");
        const { DEFAULT_CALL_SETTINGS } = await import("@/lib/webhookSync");
        const base = {
            name: "LSQ",
            workflow_id: 7,
            auth_type: "api_key" as const,
            is_active: true,
            auto_call: true,
            field_mapping: { custom: {} },
            call_settings: DEFAULT_CALL_SETTINGS,
            rate_limit_per_minute: 100,
        };
        expect(endpointFormErrors(base)).toEqual([]);
        expect(endpointFormErrors({ ...base, name: " ", workflow_id: 0 })).toEqual([
            "Give the endpoint a name",
            "Choose the agent that calls these leads",
        ]);
        expect(
            endpointFormErrors({
                ...base,
                call_settings: {
                    ...DEFAULT_CALL_SETTINGS,
                    calling_hours: { ...DEFAULT_CALL_SETTINGS.calling_hours, start: "21:00", end: "09:00", days: [] },
                    callback_url: "http://crm.example.com",
                },
            }),
        ).toEqual([
            "Calling hours must end after they start",
            "Pick at least one calling day",
            "The callback URL must start with https://",
        ]);
    });

    it("offers retries on no answer and busy only", async () => {
        await renderForm();
        expect(screen.getByText("No answer")).toBeTruthy();
        expect(screen.getByText("Busy")).toBeTruthy();
        expect(screen.queryByText("Failed")).toBeNull();
    });

    it("edits the name, rate limit, attempts and callback URL", async () => {
        const { onChange, value } = await renderForm();
        fireEvent.change(screen.getByLabelText("Name"), { target: { value: "LeadSquared" } });
        expect(onChange).toHaveBeenLastCalledWith({ ...value, name: "LeadSquared" });
        fireEvent.change(screen.getByLabelText(/Rate limit/), { target: { value: "5000" } });
        expect(onChange.mock.lastCall![0].rate_limit_per_minute).toBe(1000); // clamped
        fireEvent.change(screen.getByLabelText(/Call attempts per lead/), { target: { value: "4" } });
        expect(onChange.mock.lastCall![0].call_settings.retries.max_attempts).toBe(4);
        fireEvent.change(screen.getByLabelText(/Callback URL/), { target: { value: "  https://crm.example.com/r " } });
        expect(onChange.mock.lastCall![0].call_settings.callback_url).toBe("https://crm.example.com/r");
    });

    it("edits alert emails as typed, keeping only valid ones, and the re-enquiry window", async () => {
        const { onChange } = await renderForm();
        const input = screen.getByLabelText("Alert emails") as HTMLInputElement;
        fireEvent.change(input, { target: { value: "Ops@Client.com, nope" } });
        expect(input.value).toBe("Ops@Client.com, nope"); // kept as typed
        expect(onChange.mock.lastCall![0].call_settings.alert_emails).toEqual(["ops@client.com"]);
        expect(screen.getByText("Not a valid email: nope")).toBeTruthy();
        fireEvent.change(screen.getByLabelText(/new enquiry after/), { target: { value: "0" } });
        expect(onChange.mock.lastCall![0].call_settings.reenquiry_days).toBe(1); // clamped
    });

    it("says what pausing and auto-call off really do", async () => {
        await renderForm();
        expect(screen.getByText(/keeps them on hold, uncalled/)).toBeTruthy();
        expect(screen.getByText(/calls waiting to go out are cancelled/)).toBeTruthy();
    });

    it("warns when there is no telephony configuration to call from", async () => {
        sdk.listTelephonyConfigurationsApiV1OrganizationsTelephonyConfigsGet.mockResolvedValue({ data: { configurations: [] } });
        await renderForm();
        expect(await screen.findByText(/No telephony configuration yet/)).toBeTruthy();
    });
});

// ---- Setup ----

describe("SetupPanel", () => {
    it("hides the secret until asked, including in the test command", async () => {
        const { SetupPanel } = await import("./SetupPanel");
        render(<SetupPanel endpoint={endpoint} onChange={vi.fn()} />);
        expect(screen.queryByText(endpoint.secret)).toBeNull();
        expect(screen.getByText(/•+abcd/)).toBeTruthy();
        const command = () => document.querySelector("pre")!.textContent ?? "";
        expect(command()).toContain("X-API-Key: <secret>");
        expect(command()).not.toContain(endpoint.secret);

        fireEvent.click(screen.getByLabelText("Show secret"));
        expect(screen.getByText(endpoint.secret)).toBeTruthy();
        expect(command()).toContain(`X-API-Key: ${endpoint.secret}`);
    });

    it("regenerates the secret only after confirming", async () => {
        const rotated = { ...endpoint, secret: "newsecret0000000000000000" };
        api.regenerateSecret.mockResolvedValue(rotated);
        const onChange = vi.fn();
        const { SetupPanel } = await import("./SetupPanel");
        render(<SetupPanel endpoint={endpoint} onChange={onChange} />);
        fireEvent.click(screen.getByText("Regenerate secret"));
        expect(api.regenerateSecret).not.toHaveBeenCalled();
        fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
        await waitFor(() => expect(onChange).toHaveBeenCalledWith(rotated));
        expect(api.regenerateSecret).toHaveBeenCalledWith(3);
    });

    it("hides a URL token too", async () => {
        const { SetupPanel } = await import("./SetupPanel");
        render(
            <SetupPanel
                endpoint={{ ...endpoint, auth_type: "url_token", webhook_url: `${endpoint.webhook_url}?token=tok123` }}
                onChange={vi.fn()}
            />,
        );
        expect(screen.queryByText(/tok123/)).toBeNull();
        expect(screen.getByText(`${endpoint.webhook_url}?token=••••`)).toBeTruthy();
    });
});

// ---- Logs and history ----

describe("RequestLogsPanel", () => {
    it("lists requests and expands one to its headers and pretty body", async () => {
        api.listRequestLogs.mockResolvedValue({
            total: 1,
            logs: [
                {
                    id: 9,
                    endpoint_id: 3,
                    method: "POST",
                    content_type: "application/json",
                    headers: { "x-api-key": "[redacted]" },
                    raw_body: '{"mobile":"9876543210"}',
                    body_truncated: false,
                    response_code: 401,
                    error: "Wrong API key",
                    ip: "14.97.48.14",
                    lead_ids: null,
                    duration_ms: 41,
                    received_at: "2026-09-28T05:10:00Z",
                },
            ],
        });
        const { RequestLogsPanel } = await import("./RequestLogsPanel");
        render(<RequestLogsPanel endpointId={3} />);
        expect(await screen.findByText("Wrong API key")).toBeTruthy();
        expect(screen.getByText("401")).toBeTruthy();
        expect(screen.getByText("41 ms")).toBeTruthy();
        fireEvent.click(screen.getByText("Wrong API key"));
        expect(screen.getByText(/\[redacted\]/)).toBeTruthy();
        expect(screen.getByText(/"mobile": "9876543210"/)).toBeTruthy();
    });
});

describe("AuditPanel", () => {
    it("shows who changed what", async () => {
        const entries: AuditEntry[] = [
            {
                id: 2,
                endpoint_id: 3,
                endpoint_name: "LeadSquared",
                user_id: 1,
                user_email: "ops@botrix.test",
                action: "updated",
                changes: { name: { from: "LSQ", to: "LeadSquared" }, auto_call: { from: true, to: false } },
                created_at: "2026-09-28T05:10:00Z",
            },
            { id: 1, endpoint_id: 3, action: "calling_resumed", created_at: "2026-09-28T05:00:00Z" },
        ];
        api.listAudit.mockResolvedValue({ entries, total: 2 });
        const { AuditPanel } = await import("./AuditPanel");
        render(<AuditPanel endpointId={3} />);
        expect(await screen.findByText("Changed")).toBeTruthy();
        expect(screen.getByText(": LSQ → LeadSquared")).toBeTruthy();
        expect(screen.getByText(": On → Off")).toBeTruthy();
        expect(screen.getByText("ops@botrix.test")).toBeTruthy();
        expect(screen.getByText("Calling resumed")).toBeTruthy();
        expect(screen.getByText("System")).toBeTruthy();
    });
});

describe("AuditPanel alerts", () => {
    it("shows an alert's message and who it was emailed to", async () => {
        const entries: AuditEntry[] = [
            {
                id: 5,
                endpoint_id: 3,
                action: "alert",
                changes: {
                    kind: "requests_failing",
                    message: "The last 5 requests from your CRM were rejected.",
                    emailed_to: ["ops@client.com"],
                },
                created_at: "2026-10-02T05:10:00Z",
            },
            {
                id: 4,
                endpoint_id: 3,
                action: "alert",
                changes: { kind: "calls_not_placed", message: "Wallet empty", emailed_to: [] },
                created_at: "2026-10-02T05:00:00Z",
            },
        ];
        api.listAudit.mockResolvedValue({ entries, total: 2 });
        const { AuditPanel } = await import("./AuditPanel");
        render(<AuditPanel endpointId={3} />);
        expect(await screen.findByText("The last 5 requests from your CRM were rejected.")).toBeTruthy();
        expect(screen.getByText("Emailed to ops@client.com")).toBeTruthy();
        expect(screen.getByText(/Not emailed/)).toBeTruthy();
        expect(screen.getAllByText("Alert")).toHaveLength(2);
    });
});

// ---- Pages ----

describe("Endpoint page", { timeout: 30000 }, () => {
    it("offers to resume calling the circuit breaker stopped", async () => {
        routeParams = { endpointId: "3" };
        searchParams = new URLSearchParams({ tab: "setup" });
        api.getEndpoint.mockResolvedValue({ ...endpoint, calling_state: "paused" });
        api.resumeCalling.mockResolvedValue({ ...endpoint, calling_state: "running" });
        const { default: Page } = await import("@/app/webhook-sync/[endpointId]/page");
        render(<Page />);
        expect(await screen.findByText("Calling stopped")).toBeTruthy();
        expect(screen.getByText(/too many failed calls/)).toBeTruthy();
        await act(async () => {
            fireEvent.click(screen.getByText("Resume calling"));
        });
        await waitFor(() => expect(screen.getByText("Calling")).toBeTruthy());
        expect(api.resumeCalling).toHaveBeenCalledWith(3);
        expect(screen.queryByText("Resume calling")).toBeNull();
    });
});

describe("Endpoint page: leads on hold", { timeout: 30000 }, () => {
    it("shows a banner and calls, skips or opens the leads on hold", async () => {
        routeParams = { endpointId: "3" };
        searchParams = new URLSearchParams({ tab: "setup" });
        api.getEndpoint.mockResolvedValue({ ...endpoint, leads_on_hold: 4 });
        api.onHoldLeadsAction.mockResolvedValue({ action: "call", done: [1, 2, 3, 4], not_done: [] });
        const { default: Page } = await import("@/app/webhook-sync/[endpointId]/page");
        render(<Page />);
        expect(await screen.findByText(/arrived while this endpoint was paused and are on hold/)).toBeTruthy();

        fireEvent.click(screen.getByRole("button", { name: "Choose which to call" }));
        const url = push.mock.lastCall![0] as string;
        expect(url).toContain("/webhook-sync/3?tab=leads");
        const filters = JSON.parse(new URLSearchParams(url.split("?")[1]).get("filters")!);
        expect(filters).toEqual([{ id: "leadStatus", value: { codes: ["On hold"] } }]);

        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: "Call all 4" }));
        });
        expect(api.onHoldLeadsAction).toHaveBeenCalledWith(3, "call");
        await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith("4 leads queued for calling"));
    });

    it("can't call while paused, but can skip", async () => {
        routeParams = { endpointId: "3" };
        searchParams = new URLSearchParams({ tab: "setup" });
        api.getEndpoint.mockResolvedValue({ ...endpoint, is_active: false, leads_on_hold: 1 });
        const { default: Page } = await import("@/app/webhook-sync/[endpointId]/page");
        render(<Page />);
        expect(await screen.findByText(/Resume the endpoint to call them/)).toBeTruthy();
        expect((screen.getByRole("button", { name: "Call all 1" }) as HTMLButtonElement).disabled).toBe(true);
        expect((screen.getByRole("button", { name: "Don't call any" }) as HTMLButtonElement).disabled).toBe(false);
    });

    it("asks what to do with them right after resuming", async () => {
        routeParams = { endpointId: "3" };
        searchParams = new URLSearchParams({ tab: "setup" });
        api.getEndpoint.mockResolvedValue({ ...endpoint, is_active: false, leads_on_hold: 2 });
        api.updateEndpoint.mockResolvedValue({ ...endpoint, is_active: true, leads_on_hold: 2 });
        api.onHoldLeadsAction.mockResolvedValue({ action: "skip", done: [1, 2], not_done: [] });
        const { default: Page } = await import("@/app/webhook-sync/[endpointId]/page");
        render(<Page />);
        const resumeButton = await screen.findByRole("button", { name: "Resume" });
        await act(async () => {
            fireEvent.click(resumeButton);
        });
        expect(api.updateEndpoint).toHaveBeenCalledWith(3, { is_active: true });
        const dialog = await screen.findByRole("alertdialog");
        expect(within(dialog).getByText("2 leads arrived while this endpoint was paused")).toBeTruthy();
        await act(async () => {
            fireEvent.click(within(dialog).getByRole("button", { name: "Don't call any" }));
        });
        expect(api.onHoldLeadsAction).toHaveBeenCalledWith(3, "skip");
        await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    });

    it("doesn't ask when nothing is on hold", async () => {
        routeParams = { endpointId: "3" };
        searchParams = new URLSearchParams({ tab: "setup" });
        api.getEndpoint.mockResolvedValue({ ...endpoint, is_active: false, leads_on_hold: 0 });
        api.updateEndpoint.mockResolvedValue({ ...endpoint, is_active: true, leads_on_hold: 0 });
        const { default: Page } = await import("@/app/webhook-sync/[endpointId]/page");
        render(<Page />);
        const resumeButton = await screen.findByRole("button", { name: "Resume" });
        await act(async () => {
            fireEvent.click(resumeButton);
        });
        expect(api.updateEndpoint).toHaveBeenCalled();
        expect(screen.queryByRole("alertdialog")).toBeNull();
        expect(screen.queryByText(/on hold/)).toBeNull();
    });
});

describe("Lead page", { timeout: 30000 }, () => {
    it("shows the timeline, the call, the CRM callback and the payload", async () => {
        routeParams = { leadId: "50" };
        api.getLead.mockResolvedValue(
            lead({
                external_lead_id: "P-1",
                disposition: "Interested",
                last_call_status: "completed",
                raw_payload: { Phone: "9876543210" },
                callback: { status: "dead_letter", attempts: 5, last_status_code: 500, last_error: "HTTP 500: boom" },
            }),
        );
        api.getEndpoint.mockResolvedValue(endpoint);
        const { default: Page } = await import("@/app/webhook-sync/leads/[leadId]/page");
        render(<Page />);
        expect(await screen.findByText("Received from the CRM")).toBeTruthy();
        expect(screen.getByText("Called 1 time")).toBeTruthy();
        expect(screen.getByText("Done")).toBeTruthy();
        expect(screen.getByText("Failed (gave up)")).toBeTruthy();
        expect(screen.getByText(/HTTP 500: boom/)).toBeTruthy();
        expect(screen.getByText(/"Phone": "9876543210"/)).toBeTruthy();
        expect(await screen.findByText("Transcript & recording")).toBeTruthy();
    });

    it("withholds the payload when numbers are masked", async () => {
        routeParams = { leadId: "50" };
        api.getLead.mockResolvedValue(lead({ phone: "+91******3210", phone_masked: true, raw_payload: null }));
        api.getEndpoint.mockResolvedValue(endpoint);
        const { default: Page } = await import("@/app/webhook-sync/leads/[leadId]/page");
        render(<Page />);
        expect(await screen.findByText(/Hidden because phone numbers are masked/)).toBeTruthy();
    });
});

// ---- Dashboard actions ----

describe("describeTestResult", () => {
    it("explains each kind of reply", async () => {
        const { describeTestResult } = await import("./TestLeadCard");
        expect(describeTestResult({ status_code: 200, body: { created: 1 }, lead_id: 5 })).toMatchObject({ ok: true });
        expect(describeTestResult({ status_code: 200, body: { invalid: 1 } }).text).toMatch(/not a valid Indian mobile/);
        expect(describeTestResult({ status_code: 200, body: { duplicates: 1 } }).text).toMatch(/duplicate/);
        expect(describeTestResult({ status_code: 404, body: { error: "Webhook endpoint is paused" } })).toEqual({
            ok: false,
            text: "Not accepted: Webhook endpoint is paused",
        });
    });
});

describe("LeadActions", () => {
    it("offers call again and stop for a finished lead, and resend when the CRM callback gave up", async () => {
        const { LeadActions } = await import("./LeadActions");
        render(
            <LeadActions
                lead={lead({ status: "no_answer", callback: { status: "dead_letter", attempts: 5 } })}
                onChanged={vi.fn()}
            />,
        );
        expect(screen.getByText("Call again")).toBeTruthy();
        expect(screen.getByText("Resend to CRM")).toBeTruthy();
        expect(screen.getByText("Stop calling")).toBeTruthy();
    });

    it("offers nothing for a duplicate, and no call again while a call is queued", async () => {
        const { LeadActions } = await import("./LeadActions");
        const { container } = render(<LeadActions lead={lead({ status: "duplicate" })} onChanged={vi.fn()} />);
        expect(container.textContent).toBe("");
        render(<LeadActions lead={lead({ status: "queued" })} onChanged={vi.fn()} />);
        expect(screen.queryByText("Call again")).toBeNull();
        expect(screen.getByText("Stop calling")).toBeTruthy();
    });
});
