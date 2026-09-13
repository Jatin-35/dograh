/** Pure thread-state logic for the assistant chat.
 *
 * Extracted from the hook so it can be tested directly: this is where the
 * step grouping and the reopen-replay live, and both have produced real bugs
 * (steps that never settled, older turns dropped on reopen).
 */

import type { WorkflowGenChatSessionResponse } from "@/client/types.gen";

import type { WorkflowGenEvent, WorkflowGenRawMessage, WorkflowGenThreadItem } from "./types";

/** Shown from the moment a turn starts until the backend reports its first
 * real step, so the gap while the model decides what to do isn't silent. */
export const THINKING = "Thinking…";

/** Add a step to the turn's in-progress group, starting one if needed.
 *
 * The placeholder is dropped as soon as a real step arrives, and consecutive
 * duplicates are collapsed — the backend legitimately emits the same label for
 * different tools (several map to "Checking available node types…"). */
export function appendStep(thread: WorkflowGenThreadItem[], step: string): WorkflowGenThreadItem[] {
    const last = thread[thread.length - 1];
    if (last?.kind === "steps" && last.running) {
        const kept = step === THINKING ? last.steps : last.steps.filter((s) => s !== THINKING);
        if (kept[kept.length - 1] === step) return thread;
        return [...thread.slice(0, -1), { ...last, steps: [...kept, step] }];
    }
    return [...thread, { id: `steps-${Date.now()}-${thread.length}`, kind: "steps", steps: [step], running: true }];
}

/** Mark the turn's step group finished. A group left `running` would spin
 * forever if a turn ended without a reply (an aborted stream, say). */
export function settleSteps(thread: WorkflowGenThreadItem[]): WorkflowGenThreadItem[] {
    const last = thread[thread.length - 1];
    if (last?.kind !== "steps" || !last.running) return thread;
    // A group holding only the placeholder did no observable work — drop it
    // rather than leave "Thinking…" sitting in the transcript.
    const steps = last.steps.filter((s) => s !== THINKING);
    if (steps.length === 0) return thread.slice(0, -1);
    return [...thread.slice(0, -1), { ...last, steps, running: false }];
}

/** Fold one non-status event into the thread.
 *
 * Shared by the live SSE stream and the replay that rebuilds a reopened
 * thread, so the two can't drift into showing different things. */
export function applyThreadEvent(
    thread: WorkflowGenThreadItem[],
    event: WorkflowGenEvent,
    idSeed: string,
): WorkflowGenThreadItem[] {
    if (event.type === "assistant") {
        return [...thread, { id: `assistant-${idSeed}`, kind: "assistant", text: event.data.message }];
    }
    if (event.type === "approval") {
        return [
            ...thread,
            {
                id: `approval-${event.data.action_id}`,
                kind: "approval",
                actionId: event.data.action_id,
                actionType: event.data.action_type,
                summary: event.data.summary,
                definitionPreview: event.data.definition_preview,
                resolved: false,
            },
        ];
    }
    if (event.type === "workflow_ready") {
        return [
            // Reaching a result means the approval above it went through.
            ...thread.map((item) =>
                item.kind === "approval" && !item.resolved ? { ...item, resolved: true } : item,
            ),
            {
                id: `workflow-${event.data.workflow_id}-${idSeed}`,
                kind: "workflow_ready",
                workflowId: event.data.workflow_id,
                name: event.data.name,
                nodeCount: event.data.node_count,
                edgeCount: event.data.edge_count,
                url: event.data.url,
            },
        ];
    }
    if (event.type === "error") {
        return [...thread, { id: `error-${idSeed}`, kind: "error", code: event.data.code, text: event.data.message }];
    }
    return thread;
}

/** Settle one approval card once the user has confirmed or declined it.
 *
 * An unresolved approval disables the composer, and only a `workflow_ready`
 * event used to resolve one — so Cancel, and approving anything that doesn't
 * build a workflow (creating a tool or a credential), left the card open and
 * the panel unusable until a reload. Resolving on the user's action covers
 * every action type and both outcomes.
 */
export function resolveApproval(
    thread: WorkflowGenThreadItem[],
    actionId: string,
): WorkflowGenThreadItem[] {
    return thread.map((item) =>
        item.kind === "approval" && item.actionId === actionId ? { ...item, resolved: true } : item,
    );
}

/** Rebuild the thread from the session's persisted SSE frames.
 *
 * Replayed through the same reducers that build the thread live, so a
 * reopened conversation shows exactly what it showed originally — steps and
 * cards included — rather than the two drifting apart. Returns null for
 * sessions predating the event log, which fall back to the transcript. */
export function threadFromEvents(session: WorkflowGenChatSessionResponse): WorkflowGenThreadItem[] | null {
    const events = (session.events ?? []) as unknown as Array<{ type: string; data: Record<string, unknown> }>;
    if (events.length === 0) return null;

    let items: WorkflowGenThreadItem[] = [];
    for (const event of events) {
        if (event.type === "user") {
            items = [...items, { id: `ev-user-${items.length}`, kind: "user", text: String(event.data.message ?? "") }];
        } else if (event.type === "status") {
            items = appendStep(items, String(event.data.message ?? ""));
        } else {
            items = settleSteps(items);
            items = applyThreadEvent(items, event as unknown as WorkflowGenEvent, `ev-${items.length}`);
        }
    }
    items = settleSteps(items);

    // `pending_action` is the authority on what's still open: any approval
    // replayed without one outstanding was already resolved, one way or the
    // other (the decline path emits no event of its own).
    if (!session.pending_action) {
        return items.map((item) => (item.kind === "approval" ? { ...item, resolved: true } : item));
    }

    // The server says something is awaiting a decision, so a card must be on
    // screen to make it. If the event that produced it aged out of the capped
    // log, rebuild it from `pending_action` — otherwise the composer looks
    // usable while the server rejects every message, with no way out at all.
    const alreadyShown = items.some((item) => item.kind === "approval" && !item.resolved);
    return alreadyShown ? items : [...items, approvalFromPendingAction(session)!];
}

type PendingAction = {
    action_id: string;
    action_type: Extract<WorkflowGenThreadItem, { kind: "approval" }>["actionType"];
    preview?: Record<string, unknown>;
};

/** The approval card described by a session's `pending_action`, if any.
 *
 * Renders `preview` — the server-masked view — never `arguments`, which holds
 * raw values including secrets. */
export function approvalFromPendingAction(
    session: WorkflowGenChatSessionResponse,
): WorkflowGenThreadItem | null {
    const pending = session.pending_action as PendingAction | null | undefined;
    if (!pending) return null;
    return {
        id: `pending-${pending.action_id}`,
        kind: "approval",
        actionId: pending.action_id,
        actionType: pending.action_type,
        summary: "Review the proposed action before it runs.",
        definitionPreview: pending.preview ?? {},
        resolved: false,
    };
}

/** Plain-text items from the model transcript, for turns the event log
 * doesn't cover. `skipUserTurns` drops the leading turns that *are* covered,
 * so the two halves meet without duplicating anything. */
export function threadFromTranscript(
    session: WorkflowGenChatSessionResponse,
    skipFromUserTurn = Number.POSITIVE_INFINITY,
): WorkflowGenThreadItem[] {
    const items: WorkflowGenThreadItem[] = [];
    const messages = (session.messages ?? []) as unknown as WorkflowGenRawMessage[];
    let userTurns = 0;

    messages.forEach((message, index) => {
        if (message.role === "user" && message.content) {
            userTurns += 1;
            if (userTurns > skipFromUserTurn) return;
            items.push({ id: `restored-${index}`, kind: "user", text: message.content });
        } else if (message.role === "assistant" && message.content) {
            if (userTurns > skipFromUserTurn) return;
            items.push({ id: `restored-${index}`, kind: "assistant", text: message.content });
        }
    });
    return items;
}

export function threadFromSession(session: WorkflowGenChatSessionResponse): WorkflowGenThreadItem[] {
    const replayed = threadFromEvents(session);
    if (replayed) {
        // The event log began mid-conversation for threads that existed before
        // it was added, and it's capped, so it can cover only the tail. Show
        // the earlier turns as plain text rather than dropping them.
        const events = (session.events ?? []) as unknown as Array<{ type: string }>;
        const coveredTurns = events.filter((e) => e.type === "user").length;
        const totalTurns = ((session.messages ?? []) as unknown as WorkflowGenRawMessage[]).filter(
            (m) => m.role === "user" && m.content,
        ).length;
        const older = totalTurns > coveredTurns ? threadFromTranscript(session, totalTurns - coveredTurns) : [];
        return [...older, ...replayed];
    }

    // No event log at all (a thread from before the feature): text only, plus
    // any card still awaiting a decision so it stays actionable.
    const items = threadFromTranscript(session);
    const pending = approvalFromPendingAction(session);
    return pending ? [...items, pending] : items;
}
