import { describe, expect, it } from "vitest";

import {
    appendStep,
    applyThreadEvent,
    resolveApproval,
    settleSteps,
    THINKING,
    threadFromSession,
} from "./threadState";
import type { WorkflowGenEvent, WorkflowGenThreadItem } from "./types";

type Session = Parameters<typeof threadFromSession>[0];

/** Minimal session shape; the helpers only read these four fields. */
function session(parts: {
    events?: Array<{ type: string; data: Record<string, unknown> }>;
    messages?: Array<{ role: string; content?: string | null }>;
    pending?: Record<string, unknown> | null;
}): Session {
    return {
        id: 1,
        session_uuid: "u",
        revision: 0,
        status: "idle",
        messages: parts.messages ?? [],
        events: parts.events ?? [],
        pending_action: parts.pending ?? null,
    } as unknown as Session;
}

const userEvent = (message: string) => ({ type: "user", data: { message } });
const statusEvent = (message: string) => ({ type: "status", data: { message } });
const assistantEvent = (message: string) => ({ type: "assistant", data: { message } });
const approvalEvent = (actionId: string) => ({
    type: "approval",
    data: {
        action_id: actionId,
        action_type: "save_workflow",
        summary: "Ready to save",
        definition_preview: { changes: ["Edits “A”: prompt (rewritten)"] },
    },
});

function stepsOf(thread: WorkflowGenThreadItem[]) {
    return thread.filter((i): i is Extract<WorkflowGenThreadItem, { kind: "steps" }> => i.kind === "steps");
}

describe("appendStep", () => {
    it("starts a group and keeps appending to it within one turn", () => {
        let thread = appendStep([], "Reading the workflow…");
        thread = appendStep(thread, "Saving the workflow…");
        expect(stepsOf(thread)).toHaveLength(1);
        expect(stepsOf(thread)[0].steps).toEqual(["Reading the workflow…", "Saving the workflow…"]);
        expect(stepsOf(thread)[0].running).toBe(true);
    });

    it("drops the thinking placeholder once real work is reported", () => {
        let thread = appendStep([], THINKING);
        thread = appendStep(thread, "Reading the workflow…");
        expect(stepsOf(thread)[0].steps).toEqual(["Reading the workflow…"]);
    });

    it("collapses a repeated step — several tools share one label", () => {
        let thread = appendStep([], "Checking available node types…");
        thread = appendStep(thread, "Checking available node types…");
        expect(stepsOf(thread)[0].steps).toEqual(["Checking available node types…"]);
    });

    it("starts a fresh group for the next turn rather than reopening a settled one", () => {
        let thread = settleSteps(appendStep([], "Saving the workflow…"));
        thread = appendStep(thread, "Reading the workflow…");
        expect(stepsOf(thread)).toHaveLength(2);
        expect(stepsOf(thread)[0].running).toBe(false);
        expect(stepsOf(thread)[1].running).toBe(true);
    });
});

describe("settleSteps", () => {
    it("marks the group finished so it stops showing a live indicator", () => {
        const thread = settleSteps(appendStep([], "Saving the workflow…"));
        expect(stepsOf(thread)[0].running).toBe(false);
    });

    it("removes a group that only ever said it was thinking", () => {
        expect(settleSteps(appendStep([], THINKING))).toEqual([]);
    });

    it("is safe to call repeatedly and on threads with no group", () => {
        const once = settleSteps(appendStep([], "Saving the workflow…"));
        expect(settleSteps(once)).toEqual(once);
        expect(settleSteps([])).toEqual([]);
    });
});

describe("applyThreadEvent", () => {
    it("resolves an open approval when the result arrives", () => {
        let thread = applyThreadEvent([], approvalEvent("a1") as unknown as WorkflowGenEvent, "s1");
        expect(thread.find((i) => i.kind === "approval")).toMatchObject({ resolved: false });

        thread = applyThreadEvent(
            thread,
            {
                type: "workflow_ready",
                data: { workflow_id: 7, name: "Flow", node_count: 4, edge_count: 3, valid: true, url: "/workflow/7" },
            } as WorkflowGenEvent,
            "s2",
        );
        expect(thread.find((i) => i.kind === "approval")).toMatchObject({ resolved: true });
        expect(thread.at(-1)).toMatchObject({ kind: "workflow_ready", workflowId: 7 });
    });

    it("ignores the done frame, which carries nothing to render", () => {
        const thread = [{ id: "a", kind: "assistant", text: "hi" }] as WorkflowGenThreadItem[];
        expect(applyThreadEvent(thread, { type: "done", data: {} } as WorkflowGenEvent, "s")).toEqual(thread);
    });
});

describe("resolveApproval — acting on a card must unblock the composer", () => {
    /** An unresolved approval disables the composer, so a card that never
     * settles makes the panel unusable until a reload. */
    const openApproval = (id: string) =>
        applyThreadEvent([], approvalEvent(id) as unknown as WorkflowGenEvent, `s-${id}`);

    it("settles the card when the user cancels", () => {
        const thread = resolveApproval(openApproval("a1"), "a1");
        expect(thread.find((i) => i.kind === "approval")).toMatchObject({ resolved: true });
    });

    it("settles it when the approved action builds no workflow", () => {
        // Creating a tool or a credential emits no workflow_ready, which used
        // to be the only thing that resolved a card.
        const thread = resolveApproval(openApproval("a2"), "a2");
        expect(thread.some((i) => i.kind === "approval" && !i.resolved)).toBe(false);
    });

    it("leaves other cards alone", () => {
        let thread = openApproval("a1");
        thread = applyThreadEvent(thread, approvalEvent("a2") as unknown as WorkflowGenEvent, "s2");
        thread = resolveApproval(thread, "a1");
        const byId = Object.fromEntries(
            thread
                .filter((i): i is Extract<WorkflowGenThreadItem, { kind: "approval" }> => i.kind === "approval")
                .map((i) => [i.actionId, i.resolved]),
        );
        expect(byId).toEqual({ a1: true, a2: false });
    });

    it("is a no-op for an unknown action id", () => {
        const thread = openApproval("a1");
        expect(resolveApproval(thread, "nope")).toEqual(thread);
    });
});

describe("threadFromSession — reopening a thread", () => {
    it("replays a recorded conversation including its steps and cards", () => {
        const thread = threadFromSession(
            session({
                events: [
                    userEvent("shorten the greeting"),
                    statusEvent("Reading the workflow…"),
                    statusEvent("Saving the workflow…"),
                    approvalEvent("a1"),
                    assistantEvent("Saved as a draft."),
                ],
                messages: [{ role: "user", content: "shorten the greeting" }],
            }),
        );
        expect(thread.map((i) => i.kind)).toEqual(["user", "steps", "approval", "assistant"]);
        expect(stepsOf(thread)[0].running).toBe(false);
        // No pending action means that approval was already resolved.
        expect(thread.find((i) => i.kind === "approval")).toMatchObject({ resolved: true });
    });

    it("leaves an approval open when one is still pending", () => {
        const thread = threadFromSession(
            session({
                events: [userEvent("add a tool"), approvalEvent("a1")],
                messages: [{ role: "user", content: "add a tool" }],
                pending: { action_id: "a1" },
            }),
        );
        expect(thread.find((i) => i.kind === "approval")).toMatchObject({ resolved: false });
    });

    it("keeps older turns that predate the event log instead of dropping them", () => {
        // The regression: a thread with history from before events were
        // recorded showed only the most recent turn.
        const thread = threadFromSession(
            session({
                messages: [
                    { role: "user", content: "first question" },
                    { role: "assistant", content: "first answer" },
                    { role: "user", content: "second question" },
                    { role: "assistant", content: "second answer" },
                ],
                events: [userEvent("second question"), assistantEvent("second answer")],
            }),
        );
        expect(thread.map((i) => ("text" in i ? i.text : i.kind))).toEqual([
            "first question",
            "first answer",
            "second question",
            "second answer",
        ]);
    });

    it("always shows a card when the server says one is pending", () => {
        // The capped event log can age out the approval frame while the server
        // still holds the pending action. Without a card the composer looks
        // usable but every message is rejected — and a reload doesn't help.
        const thread = threadFromSession(
            session({
                events: [userEvent("do the thing"), assistantEvent("working on it")],
                messages: [{ role: "user", content: "do the thing" }],
                pending: { action_id: "orphaned", action_type: "save_workflow", preview: { name: "Flow" } },
            }),
        );
        const open = thread.filter((i) => i.kind === "approval" && !i.resolved);
        expect(open).toHaveLength(1);
        expect(open[0]).toMatchObject({ actionId: "orphaned" });
    });

    it("does not add a second card when the pending one already replayed", () => {
        const thread = threadFromSession(
            session({
                events: [userEvent("do the thing"), approvalEvent("a1")],
                messages: [{ role: "user", content: "do the thing" }],
                pending: { action_id: "a1", action_type: "save_workflow" },
            }),
        );
        expect(thread.filter((i) => i.kind === "approval")).toHaveLength(1);
    });

    it("falls back to transcript text for a session recorded before the event log", () => {
        const thread = threadFromSession(
            session({
                messages: [
                    { role: "user", content: "hello" },
                    { role: "assistant", content: "hi there" },
                    { role: "tool", content: "{}" },
                    { role: "system", content: "ignored" },
                ],
            }),
        );
        expect(thread.map((i) => i.kind)).toEqual(["user", "assistant"]);
    });

    it("restores a pending approval for a pre-event-log session so it stays actionable", () => {
        const thread = threadFromSession(
            session({
                messages: [{ role: "user", content: "add a tool" }],
                pending: { action_id: "a9", action_type: "create_tool", preview: { name: "T" } },
            }),
        );
        expect(thread.at(-1)).toMatchObject({ kind: "approval", actionId: "a9", resolved: false });
    });

    it("never renders raw arguments, which can hold secrets", () => {
        const thread = threadFromSession(
            session({
                messages: [{ role: "user", content: "store my key" }],
                pending: {
                    action_id: "a1",
                    action_type: "create_credential",
                    arguments: { credential_data: { api_key: "SUPER-SECRET" } },
                    preview: { credential_data: { api_key: "****CRET" } },
                },
            }),
        );
        expect(JSON.stringify(thread)).not.toContain("SUPER-SECRET");
    });
});
