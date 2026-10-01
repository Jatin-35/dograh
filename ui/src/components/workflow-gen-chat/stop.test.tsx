import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
    ensureSession: vi.fn(),
    getSession: vi.fn(),
    toastError: vi.fn(),
}));

vi.mock("@/client/sdk.gen", () => ({
    createWorkflowGenSessionApiV1WorkflowGenSessionsPost: vi.fn(),
    ensureWorkflowGenSessionForWorkflowApiV1WorkflowGenWorkflowsWorkflowIdSessionPost: (...a: unknown[]) =>
        h.ensureSession(...a),
    getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet: (...a: unknown[]) => h.getSession(...a),
}));
vi.mock("@/client/client.gen", () => ({ client: { getConfig: () => ({ baseUrl: "http://api" }) } }));
vi.mock("@/lib/auth", () => ({
    useAuth: () => ({ isAuthenticated: true, loading: false, getAccessToken: async () => "token" }),
}));
vi.mock("sonner", () => ({ toast: { error: (...a: unknown[]) => h.toastError(...a) } }));

import { useWorkflowGenChatSession } from "./useWorkflowGenChatSession";

const SESSION = {
    id: 7,
    revision: 3,
    status: "idle",
    messages: [],
    events: [],
    pending_action: null,
    workflow_id: 11,
};

const encoder = new TextEncoder();
const frame = (event: object) => encoder.encode(`data: ${JSON.stringify(event)}\n\n`);

/** A reply that sends one step and then keeps "working" until aborted. */
function hangingStream(signal: AbortSignal) {
    return new ReadableStream<Uint8Array>({
        start(controller) {
            controller.enqueue(frame({ type: "status", data: { message: "Reading the workflow" }, revision: 4 }));
            signal.addEventListener("abort", () =>
                controller.error(new DOMException("The operation was aborted.", "AbortError")),
            );
        },
    });
}

let fetchCalls: Array<{ url: string; body: Record<string, unknown>; signal: AbortSignal }>;

beforeEach(() => {
    fetchCalls = [];
    h.ensureSession.mockReset();
    h.getSession.mockReset();
    h.ensureSession.mockResolvedValue({ data: SESSION });
    h.getSession.mockResolvedValue({ data: { ...SESSION, revision: 4, status: "idle" } });
    h.toastError.mockReset();
    vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, init: RequestInit) => {
            const signal = init.signal as AbortSignal;
            fetchCalls.push({ url, body: JSON.parse(String(init.body)), signal });
            return new Response(hangingStream(signal), { status: 200 });
        }),
    );
});

afterEach(() => {
    vi.unstubAllGlobals();
});

async function loaded() {
    const hook = renderHook(() => useWorkflowGenChatSession({ workflowId: 11 }));
    await waitFor(() => expect(hook.result.current.session?.id).toBe(7));
    return hook;
}

const stepsText = (thread: ReturnType<typeof useWorkflowGenChatSession>["thread"]) =>
    thread.flatMap((item) => (item.kind === "steps" ? item.steps : []));

describe("Stop", () => {
    it("cancels the reply, shows Stopped, raises no error, and refreshes the revision", async () => {
        const { result } = await loaded();

        let sending!: Promise<void>;
        act(() => {
            sending = result.current.sendMessage("add a transfer tool");
        });
        await waitFor(() => expect(stepsText(result.current.thread)).toContain("Reading the workflow"));
        expect(result.current.sendingMessage).toBe(true);

        await act(async () => {
            result.current.stop();
            await sending;
        });

        expect(fetchCalls[0].signal.aborted).toBe(true);
        expect(result.current.sendingMessage).toBe(false);
        expect(stepsText(result.current.thread)).toContain("Stopped");
        expect(h.toastError).not.toHaveBeenCalled();
        expect(h.getSession).toHaveBeenCalledWith({ path: { session_id: 7 } });
        expect(result.current.session?.revision).toBe(4);
    });

    it("sends the next message with the refreshed revision, so it isn't refused", async () => {
        const { result } = await loaded();
        let sending!: Promise<void>;
        act(() => {
            sending = result.current.sendMessage("first");
        });
        await waitFor(() => expect(fetchCalls).toHaveLength(1));
        await act(async () => {
            result.current.stop();
            await sending;
        });

        act(() => {
            void result.current.sendMessage("second");
        });
        await waitFor(() => expect(fetchCalls).toHaveLength(2));
        expect(fetchCalls[1].body).toEqual({ text: "second", expected_revision: 4 });
    });

    it("stops an approved action too", async () => {
        const { result } = await loaded();
        let confirming!: Promise<void>;
        act(() => {
            confirming = result.current.confirmPendingAction("a1", true);
        });
        await waitFor(() => expect(result.current.confirming).toBe(true));
        await act(async () => {
            result.current.stop();
            await confirming;
        });
        expect(fetchCalls[0].url).toContain("/confirm");
        expect(result.current.confirming).toBe(false);
        expect(stepsText(result.current.thread)).toContain("Stopped");
        expect(h.toastError).not.toHaveBeenCalled();
    });

    it("is harmless when nothing is running, and a double press does no harm", async () => {
        const { result } = await loaded();
        act(() => result.current.stop()); // nothing in flight
        expect(h.getSession).not.toHaveBeenCalled();

        let sending!: Promise<void>;
        act(() => {
            sending = result.current.sendMessage("hi");
        });
        await waitFor(() => expect(fetchCalls).toHaveLength(1));
        await act(async () => {
            result.current.stop();
            result.current.stop();
            await sending;
        });
        expect(stepsText(result.current.thread).filter((s) => s === "Stopped")).toHaveLength(1);
        expect(h.toastError).not.toHaveBeenCalled();
    });

    it("a real failure still shows an error toast", async () => {
        vi.stubGlobal("fetch", vi.fn(async () => new Response("boom", { status: 500 })));
        const { result } = await loaded();
        await act(async () => {
            await result.current.sendMessage("hi");
        });
        expect(h.toastError).toHaveBeenCalledWith("Request failed: 500");
        expect(stepsText(result.current.thread)).not.toContain("Stopped");
    });

    it("if refreshing after Stop fails, the panel still recovers", async () => {
        h.getSession.mockResolvedValue({ error: { detail: "nope" } });
        const { result } = await loaded();
        let sending!: Promise<void>;
        act(() => {
            sending = result.current.sendMessage("hi");
        });
        await waitFor(() => expect(fetchCalls).toHaveLength(1));
        await act(async () => {
            result.current.stop();
            await sending;
        });
        expect(result.current.sendingMessage).toBe(false);
        expect(result.current.session?.revision).toBe(3); // unchanged, no crash
    });
});
