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

import {
    appendStep,
    applyThreadEvent,
    resolveApproval,
    settleSteps,
    STOPPED,
    THINKING,
    threadFromSession,
} from "./threadState";
import type {
    WorkflowGenEvent,
    WorkflowGenSseFrame,
    WorkflowGenThreadItem,
} from "./types";

/** Keyed per surface. One shared key meant opening Scout in the Code Editor
 * restored whatever conversation the standalone page last used — landing the
 * user in an unrelated workflow-building thread with no way to tell why. */
const sessionStorageKey = (surface: string) =>
    `dograh:workflow-gen:session-id:${surface}`;

function getErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : "Something went wrong";
}

/** The user pressed Stop: fetch rejects (or the read loop throws) with an
 * AbortError. Not a failure, so no error toast. */
function isAbort(error: unknown): boolean {
    return (error as { name?: unknown } | null)?.name === "AbortError";
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
    /** Which part of the product opened this session. Steers the prompt's
     * orientation so an ambiguous request is read the way the surface
     * implies — in the Code Editor, "add a tool" means write a function. */
    surface?: "standalone" | "code_editor";
}

export function useWorkflowGenChatSession({
    workflowId,
    enabled = true,
    surface,
}: UseWorkflowGenChatSessionOptions = {}) {
    const { isAuthenticated, loading: authLoading, getAccessToken } = useAuth();
    const [session, setSession] = useState<WorkflowGenChatSessionResponse | null>(null);
    const [thread, setThread] = useState<WorkflowGenThreadItem[]>([]);
    const [creatingSession, setCreatingSession] = useState(false);
    const [sendingMessage, setSendingMessage] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [statusMessage, setStatusMessage] = useState<string | null>(null);
    const hasStarted = useRef(false);
    // Distinguishes "still loading" from "load failed", so the panel can offer
    // a retry instead of spinning forever.
    const [loadFailed, setLoadFailed] = useState(false);

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
            setLoadFailed(false);
            setLoadFailed(false);
            try {
                let response;
                if (workflowId != null) {
                    response = await ensureWorkflowGenSessionForWorkflowApiV1WorkflowGenWorkflowsWorkflowIdSessionPost({
                        path: { workflow_id: workflowId },
                    });
                } else if (opts?.forceNew) {
                    response = await createWorkflowGenSessionApiV1WorkflowGenSessionsPost(
                        surface ? { body: { surface } } : {},
                    );
                } else if (opts?.sessionId != null) {
                    response = await getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet({
                        path: { session_id: opts.sessionId },
                    });
                } else {
                    const storageKey = sessionStorageKey(surface ?? "standalone");
                    const storedId = typeof window !== "undefined" ? window.localStorage.getItem(storageKey) : null;
                    if (storedId) {
                        response = await getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet({
                            path: { session_id: Number(storedId) },
                        });
                    } else {
                        response = await createWorkflowGenSessionApiV1WorkflowGenSessionsPost(
                        surface ? { body: { surface } } : {},
                    );
                    }
                }

                if (response.error || !response.data) {
                    throw new Error(detailFromError(response.error, "Failed to start assistant session"));
                }
                setSession(response.data);
                setThread(threadFromSession(response.data));
                if (workflowId == null && typeof window !== "undefined") {
                    window.localStorage.setItem(sessionStorageKey(surface ?? "standalone"), String(response.data.id));
                }
            } catch (error) {
                // Release the one-shot latch so the next mount (reopening the
                // panel) tries again. Without this a single failed load left a
                // spinner on screen permanently, recoverable only by reloading
                // the page.
                hasStarted.current = false;
                setLoadFailed(true);
                toast.error(getErrorMessage(error));
            } finally {
                setCreatingSession(false);
            }
        },
        [workflowId, surface],
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

    // Set when the server refuses a message because an action is still awaiting
    // a decision; `sendMessage` then reloads the session to show that card.
    const refusedForPendingRef = useRef(false);

    const handleEvent = useCallback((event: WorkflowGenEvent) => {
        if (event.type === "error" && event.data.code === "pending_action") {
            refusedForPendingRef.current = true;
            return;
        }
        if (event.type === "status") {
            setStatusMessage(event.data.message);
            // Append to the turn's step group so the work stays visible after
            // the turn ends, rather than overwriting one disposable line.
            setThread((prev) => appendStep(prev, event.data.message));
            return;
        }
        setStatusMessage(null);
        // Any non-status event means the turn produced something, so the
        // steps that led here are finished.
        setThread((prev) => applyThreadEvent(settleSteps(prev), event, `live-${Date.now()}`));
    }, []);

    // The in-flight turn's stream, so Stop can cancel it. Dropping the stream
    // is what stops the turn: the server cancels it on disconnect.
    const abortRef = useRef<AbortController | null>(null);

    const streamFrom = useCallback(
        async (path: string, body: Record<string, unknown>) => {
            const token = await getAccessToken();
            const baseUrl = client.getConfig().baseUrl ?? "";
            const controller = new AbortController();
            abortRef.current = controller;
            const response = await fetch(`${baseUrl}${path}`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
                body: JSON.stringify(body),
                signal: controller.signal,
            });
            if (!response.ok || !response.body) {
                throw new Error(`Request failed: ${response.status}`);
            }
            let lastRevision: number | null = null;
            for await (const frame of parseSseStream(response)) {
                lastRevision = frame.revision;
                handleEvent(frame);
            }
            // Error frames carry -1, meaning "no revision"; adopting it would
            // make the next message fail the revision check.
            if (lastRevision != null && lastRevision >= 0) {
                setSession((prev) => (prev ? { ...prev, revision: lastRevision } : prev));
            }
        },
        [getAccessToken, handleEvent],
    );

    /** Stop the turn in flight. */
    const stop = useCallback(() => {
        abortRef.current?.abort();
    }, []);

    /** After a Stop, the steps that did complete have advanced the session's
     * revision server-side, but this tab only learns a revision at the end of
     * a stream, so the next message would be refused as stale. Re-read it. */
    const afterStop = useCallback(async () => {
        setThread((prev) => appendStep(prev, STOPPED));
        if (!session) return;
        const response = await getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet({
            path: { session_id: session.id },
        });
        if (!response.error && response.data) {
            const fresh = response.data;
            setSession((prev) => (prev ? { ...prev, revision: fresh.revision, status: fresh.status } : prev));
        }
    }, [session]);

    /** Re-read the session and rebuild the thread from it, so the card the
     * server is waiting on is on screen (last in the thread) and the composer
     * locks until it is resolved. */
    const reloadPendingCard = useCallback(async () => {
        if (!session) return;
        const response = await getWorkflowGenSessionApiV1WorkflowGenSessionsSessionIdGet({
            path: { session_id: session.id },
        });
        if (response.error || !response.data) return;
        setSession(response.data);
        setThread(threadFromSession(response.data));
    }, [session]);

    /** Sends one user turn. Resolves false when the server refused it because
     * an action still awaits a decision, so the caller can give the text back. */
    const sendMessage = useCallback(
        async (text: string): Promise<boolean> => {
            const trimmed = text.trim();
            if (!session || !trimmed) return true;

            refusedForPendingRef.current = false;
            setThread((prev) => appendStep([...prev, { id: `user-${Date.now()}`, kind: "user", text: trimmed }], THINKING));
            setSendingMessage(true);
            try {
                await streamFrom(`/api/v1/workflow-gen/sessions/${session.id}/messages`, {
                    text: trimmed,
                    expected_revision: session.revision,
                });
            } catch (error) {
                if (isAbort(error)) await afterStop();
                else toast.error(getErrorMessage(error));
            } finally {
                abortRef.current = null;
                setSendingMessage(false);
                setStatusMessage(null);
                setThread(settleSteps);
            }
            if (refusedForPendingRef.current) {
                refusedForPendingRef.current = false;
                await reloadPendingCard();
                toast.error("Your message wasn't sent: confirm or cancel the proposed change above first.");
                return false;
            }
            return true;
        },
        [session, streamFrom, afterStop, reloadPendingCard],
    );

    const confirmPendingAction = useCallback(
        async (actionId: string, approve: boolean) => {
            if (!session) return;
            // Settle the card as soon as the user acts on it. Previously only a
            // `workflow_ready` event marked one resolved, so Cancel — and
            // approving anything that doesn't build a workflow, like creating a
            // tool — left it open forever, and an open approval disables the
            // composer. The panel became unusable until a reload.
            setThread((prev) => resolveApproval(prev, actionId));
            setThread((prev) => appendStep(prev, THINKING));
            setConfirming(true);
            try {
                await streamFrom(`/api/v1/workflow-gen/sessions/${session.id}/confirm`, {
                    action_id: actionId,
                    approve,
                });
            } catch (error) {
                if (isAbort(error)) await afterStop();
                else toast.error(getErrorMessage(error));
            } finally {
                abortRef.current = null;
                setConfirming(false);
                setStatusMessage(null);
                setThread(settleSteps);
            }
        },
        [session, streamFrom, afterStop],
    );

    return {
        session,
        thread,
        creatingSession,
        sendingMessage,
        confirming,
        statusMessage,
        loadFailed,
        retryLoadSession: () => loadSession(),
        hasPendingAction: Boolean(pendingAction),
        sendMessage,
        confirmPendingAction,
        stop,
        switchToSession,
        startNewSession,
    };
}

export type WorkflowGenChatSessionState = ReturnType<typeof useWorkflowGenChatSession>;
