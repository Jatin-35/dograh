"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { client } from "@/client/client.gen";
import {
    createWorkflowGenSessionApiV1WorkflowGenSessionsPost,
    ensureWorkflowGenSessionForWorkflowApiV1WorkflowGenWorkflowsWorkflowIdSessionPost,
    getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet,
} from "@/client/sdk.gen";
import type { WorkflowGenChatSessionResponse } from "@/client/types.gen";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

import type {
    WorkflowGenEvent,
    WorkflowGenRawMessage,
    WorkflowGenSseFrame,
    WorkflowGenThreadItem,
} from "./types";

const STANDALONE_SESSION_STORAGE_KEY = "dograh:workflow-gen:standalone-session-id";

function getErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : "Something went wrong";
}

/** Reconstruct the thread from a session's persisted state on load. Only
 * user/assistant text messages are replayed as bubbles — tool-call
 * machinery and past workflow_ready cards aren't reconstructed from the raw
 * transcript (that's a live-SSE-only presentation, not stored separately);
 * the model's own follow-up summary text typically covers what happened. */
function threadFromSession(session: WorkflowGenChatSessionResponse): WorkflowGenThreadItem[] {
    const items: WorkflowGenThreadItem[] = [];
    const messages = (session.messages ?? []) as unknown as WorkflowGenRawMessage[];

    messages.forEach((message, index) => {
        if (message.role === "user" && message.content) {
            items.push({ id: `restored-${index}`, kind: "user", text: message.content });
        } else if (message.role === "assistant" && message.content) {
            items.push({ id: `restored-${index}`, kind: "assistant", text: message.content });
        }
    });

    const pending = session.pending_action as
        | {
            action_id: string;
            action_type: "create_workflow" | "save_workflow" | "create_tool" | "create_credential";
            arguments?: Record<string, unknown>;
            preview?: Record<string, unknown>;
        }
        | null
        | undefined;
    if (pending) {
        items.push({
            id: `pending-${pending.action_id}`,
            kind: "approval",
            actionId: pending.action_id,
            actionType: pending.action_type,
            summary: "Review the proposed action before it runs.",
            // `preview` is the server-masked view; `arguments` holds raw
            // values (including secrets) and must not be rendered.
            definitionPreview: pending.preview ?? {},
            resolved: false,
        });
    }

    return items;
}

async function* parseSseStream(response: Response): AsyncGenerator<WorkflowGenSseFrame> {
    const reader = response.body?.getReader();
    if (!reader) return;
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
            const line = frame.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            try {
                yield JSON.parse(line.slice("data: ".length)) as WorkflowGenSseFrame;
            } catch {
                // malformed frame — skip rather than crash the whole stream
            }
        }
    }
}

interface UseWorkflowGenChatSessionOptions {
    workflowId?: number;
    /** When false, skips the auto-start-or-restore-on-mount effect entirely
     * — used by WorkflowGenChatPanel when it's handed an externally-owned
     * session (the standalone page instantiates the hook itself to drive
     * its thread-history sidebar) so the panel doesn't also spin up an
     * unused parallel session in the background. Defaults to true. */
    enabled?: boolean;
}

export function useWorkflowGenChatSession({ workflowId, enabled = true }: UseWorkflowGenChatSessionOptions = {}) {
    const { isAuthenticated, loading: authLoading, getAccessToken } = useAuth();
    const [session, setSession] = useState<WorkflowGenChatSessionResponse | null>(null);
    const [thread, setThread] = useState<WorkflowGenThreadItem[]>([]);
    const [creatingSession, setCreatingSession] = useState(false);
    const [sendingMessage, setSendingMessage] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [statusMessage, setStatusMessage] = useState<string | null>(null);
    const hasStarted = useRef(false);

    const pendingAction = thread.find(
        (item): item is Extract<WorkflowGenThreadItem, { kind: "approval" }> =>
            item.kind === "approval" && !item.resolved,
    );

    /** Restores the remembered/given session, or creates a fresh one.
     *  - `opts.sessionId` — load this specific session (thread-history sidebar switch).
     *  - `opts.forceNew` — always create a new standalone session ("New thread").
     *  - neither — the original mount-time behavior: per-workflow get-or-create,
     *    or standalone restore-from-localStorage-else-create. */
    const loadSession = useCallback(
        async (opts?: { sessionId?: number; forceNew?: boolean }) => {
            setCreatingSession(true);
            try {
                let response;
                if (workflowId != null) {
                    response = await ensureWorkflowGenSessionForWorkflowApiV1WorkflowGenWorkflowsWorkflowIdSessionPost({
                        path: { workflow_id: workflowId },
                    });
                } else if (opts?.forceNew) {
                    response = await createWorkflowGenSessionApiV1WorkflowGenSessionsPost({});
                } else if (opts?.sessionId != null) {
                    response = await getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet({
                        path: { session_id: opts.sessionId },
                    });
                } else {
                    const storedId = typeof window !== "undefined" ? window.localStorage.getItem(STANDALONE_SESSION_STORAGE_KEY) : null;
                    if (storedId) {
                        response = await getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet({
                            path: { session_id: Number(storedId) },
                        });
                    } else {
                        response = await createWorkflowGenSessionApiV1WorkflowGenSessionsPost({});
                    }
                }

                if (response.error || !response.data) {
                    throw new Error(detailFromError(response.error, "Failed to start assistant session"));
                }
                setSession(response.data);
                setThread(threadFromSession(response.data));
                if (workflowId == null && typeof window !== "undefined") {
                    window.localStorage.setItem(STANDALONE_SESSION_STORAGE_KEY, String(response.data.id));
                }
            } catch (error) {
                toast.error(getErrorMessage(error));
            } finally {
                setCreatingSession(false);
            }
        },
        [workflowId],
    );

    useEffect(() => {
        if (!enabled || authLoading || !isAuthenticated || hasStarted.current) return;
        hasStarted.current = true;
        void loadSession();
    }, [enabled, authLoading, isAuthenticated, loadSession]);

    /** Switch the active session (standalone thread-history sidebar only —
     * a no-op concern for the per-workflow embedded panel, which never calls
     * this). */
    const switchToSession = useCallback((sessionId: number) => loadSession({ sessionId }), [loadSession]);

    /** Start a fresh standalone session and make it active ("New thread"). */
    const startNewSession = useCallback(() => loadSession({ forceNew: true }), [loadSession]);

    const handleEvent = useCallback((event: WorkflowGenEvent) => {
        if (event.type === "status") {
            setStatusMessage(event.data.message);
            return;
        }
        setStatusMessage(null);

        if (event.type === "assistant") {
            setThread((prev) => [...prev, { id: `assistant-${Date.now()}-${prev.length}`, kind: "assistant", text: event.data.message }]);
        } else if (event.type === "approval") {
            setThread((prev) => [
                ...prev,
                {
                    id: `approval-${event.data.action_id}`,
                    kind: "approval",
                    actionId: event.data.action_id,
                    actionType: event.data.action_type,
                    summary: event.data.summary,
                    definitionPreview: event.data.definition_preview,
                    resolved: false,
                },
            ]);
        } else if (event.type === "workflow_ready") {
            setThread((prev) => [
                ...prev.map((item) =>
                    item.kind === "approval" && !item.resolved ? { ...item, resolved: true } : item,
                ),
                {
                    id: `workflow-${event.data.workflow_id}-${Date.now()}`,
                    kind: "workflow_ready",
                    workflowId: event.data.workflow_id,
                    name: event.data.name,
                    nodeCount: event.data.node_count,
                    edgeCount: event.data.edge_count,
                    url: event.data.url,
                },
            ]);
        } else if (event.type === "error") {
            setThread((prev) => [...prev, { id: `error-${Date.now()}`, kind: "error", code: event.data.code, text: event.data.message }]);
        }
    }, []);

    const streamFrom = useCallback(
        async (path: string, body: Record<string, unknown>) => {
            const token = await getAccessToken();
            const baseUrl = client.getConfig().baseUrl ?? "";
            const response = await fetch(`${baseUrl}${path}`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
                body: JSON.stringify(body),
            });
            if (!response.ok || !response.body) {
                throw new Error(`Request failed: ${response.status}`);
            }
            let lastRevision: number | null = null;
            for await (const frame of parseSseStream(response)) {
                lastRevision = frame.revision;
                handleEvent(frame);
            }
            if (lastRevision != null) {
                setSession((prev) => (prev ? { ...prev, revision: lastRevision } : prev));
            }
        },
        [getAccessToken, handleEvent],
    );

    const sendMessage = useCallback(
        async (text: string) => {
            const trimmed = text.trim();
            if (!session || !trimmed) return;

            setThread((prev) => [...prev, { id: `user-${Date.now()}`, kind: "user", text: trimmed }]);
            setSendingMessage(true);
            try {
                await streamFrom(`/api/v1/workflow-gen/sessions/${session.id}/messages`, {
                    text: trimmed,
                    expected_revision: session.revision,
                });
            } catch (error) {
                toast.error(getErrorMessage(error));
            } finally {
                setSendingMessage(false);
                setStatusMessage(null);
            }
        },
        [session, streamFrom],
    );

    const confirmPendingAction = useCallback(
        async (actionId: string, approve: boolean) => {
            if (!session) return;
            setConfirming(true);
            try {
                await streamFrom(`/api/v1/workflow-gen/sessions/${session.id}/confirm`, {
                    action_id: actionId,
                    approve,
                });
            } catch (error) {
                toast.error(getErrorMessage(error));
            } finally {
                setConfirming(false);
                setStatusMessage(null);
            }
        },
        [session, streamFrom],
    );

    return {
        session,
        thread,
        creatingSession,
        sendingMessage,
        confirming,
        statusMessage,
        hasPendingAction: Boolean(pendingAction),
        sendMessage,
        confirmPendingAction,
        switchToSession,
        startNewSession,
    };
}

export type WorkflowGenChatSessionState = ReturnType<typeof useWorkflowGenChatSession>;
