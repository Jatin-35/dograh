"use client";

import {
    AssistantRuntimeProvider,
    ComposerPrimitive,
    type ThreadMessageLike,
    useExternalStoreRuntime,
} from "@assistant-ui/react";
import { ArrowUp, Loader2, Sparkles, X } from "lucide-react";
import { useEffect, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { AssistantWave } from "./AssistantWave";
import type { WorkflowGenThreadItem } from "./types";
import { useWorkflowGenChatSession, type WorkflowGenChatSessionState } from "./useWorkflowGenChatSession";
import { WorkflowGenActionsProvider, WorkflowGenThreadView } from "./WorkflowGenThreadView";

/** The tool-call content-part shape from `ThreadMessageLike["content"]`,
 * derived from the library's own type rather than importing its transitive
 * `assistant-stream` dependency directly. Used below to cast our
 * `Record<string, unknown>` JSON blobs (which are always JSON-serializable
 * data straight off the SSE wire) into the part's `ReadonlyJSONObject` args
 * type — TypeScript can't verify that structurally through an `unknown`
 * index signature, so this is a type-only accommodation, not a real API
 * mismatch. */
type ThreadMessageLikeParts = Extract<ThreadMessageLike["content"], readonly unknown[]>[number];
type ToolCallPart = Extract<ThreadMessageLikeParts, { type: "tool-call" }>;

interface WorkflowGenChatPanelProps {
    workflowId?: number;
    onClose?: () => void;
    className?: string;
    /** Pre-instantiated session state, owned by a caller outside this
     * component. The standalone page (`app/workflow/gen-chat/page.tsx`)
     * instantiates `useWorkflowGenChatSession` itself so its thread-history
     * sidebar can drive session switching, and passes the result in here.
     * When omitted — the per-workflow embedded panel's normal usage — the
     * component instantiates its own session exactly as before. */
    session?: WorkflowGenChatSessionState;
    /** Notifies the caller while a turn is in flight, so the editor header
     * can show a working indicator even with the panel closed. */
    onBusyChange?: (busy: boolean) => void;
}

// Opened from inside a workflow's editor, the assistant's job is changing the
// flow that's already on screen — not describing a new agent from nothing.
// The standalone /workflow/gen-chat page is the build-from-scratch entry point.
const BUILD_SUGGESTIONS = [
    "Build an inbound receptionist for a dental clinic",
    "Create a cold-calling agent that books demos",
    "Build a support agent that answers billing questions",
];

const EDIT_SUGGESTIONS = [
    "Add a transfer-call tool to the closing node",
    "Change the greeting prompt to be more concise",
    "Add a node that collects the caller's email",
];

/** Maps a `WorkflowGenThreadItem` to assistant-ui's `ThreadMessageLike`. Text
 * items map straight to a text content part; the three interactive/terminal
 * item kinds (`approval`, `workflow_ready`, `error`) each become an assistant
 * message with exactly one tool-call content part, rendered by the
 * per-`toolName` components registered in WorkflowGenThreadView. */
function convertMessage(item: WorkflowGenThreadItem): ThreadMessageLike {
    switch (item.kind) {
        case "user":
            return { id: item.id, role: "user", content: [{ type: "text", text: item.text }] };
        case "assistant":
            return { id: item.id, role: "assistant", content: [{ type: "text", text: item.text }] };
        case "approval":
            return {
                id: item.id,
                role: "assistant",
                content: [
                    {
                        type: "tool-call",
                        toolCallId: item.id,
                        toolName: "reviewAction",
                        args: {
                            actionId: item.actionId,
                            actionType: item.actionType,
                            summary: item.summary,
                            definitionPreview: item.definitionPreview,
                            resolved: item.resolved,
                        } as ToolCallPart["args"],
                        result: item.resolved ? true : undefined,
                    },
                ],
            };
        case "workflow_ready":
            return {
                id: item.id,
                role: "assistant",
                content: [
                    {
                        type: "tool-call",
                        toolCallId: item.id,
                        toolName: "workflowReady",
                        args: {
                            workflowId: item.workflowId,
                            name: item.name,
                            nodeCount: item.nodeCount,
                            edgeCount: item.edgeCount,
                            url: item.url,
                        },
                        result: true,
                    },
                ],
            };
        case "error":
            return {
                id: item.id,
                role: "assistant",
                content: [
                    {
                        type: "tool-call",
                        toolCallId: item.id,
                        toolName: "reportError",
                        args: { code: item.code, text: item.text },
                        result: true,
                    },
                ],
            };
        case "steps":
            return {
                id: item.id,
                role: "assistant",
                content: [
                    {
                        type: "tool-call",
                        toolCallId: item.id,
                        toolName: "activitySteps",
                        args: { steps: item.steps, running: item.running },
                        // Only a settled group is "complete"; a running one
                        // keeps rendering its live indicator.
                        result: item.running ? undefined : true,
                    },
                ],
            };
    }
}

interface ChatComposerProps {
    placeholder: string;
    sendingMessage: boolean;
}

/** The message-input pill, shared between the docked (non-empty-thread) and
 * centered hero (empty-thread) layouts below. */
function ChatComposer({ placeholder, sendingMessage }: ChatComposerProps) {
    return (
        <div>
            <ComposerPrimitive.Root className="flex items-end gap-2 rounded-2xl border border-input bg-background p-1.5 pl-3.5 focus-within:ring-2 focus-within:ring-ring/40">
                <ComposerPrimitive.Input
                    placeholder={placeholder}
                    className="flex max-h-40 min-h-[40px] w-full flex-1 resize-none overflow-y-auto bg-transparent py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
                    rows={1}
                    maxRows={6}
                />
                <ComposerPrimitive.Send asChild>
                    <Button size="icon" className="h-8 w-8 shrink-0 rounded-lg">
                        {sendingMessage ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUp className="h-4 w-4" />}
                    </Button>
                </ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
            {/* Scout edits real workflows that answer real calls, so the
                caveat sits where it's read — under the box you type in. */}
            <p className="mt-2 text-center text-[11px] text-muted-foreground">
                Scout can make mistakes. Review changes before publishing.
            </p>
        </div>
    );
}

export function WorkflowGenChatPanel({ workflowId, onClose, className, session: externalSession, onBusyChange }: WorkflowGenChatPanelProps) {
    // `enabled: externalSession == null` — when a caller hands us a session
    // it already instantiated (the standalone page), skip this instance's
    // own auto-start-or-restore effect entirely rather than spinning up an
    // unused parallel session. Rules of Hooks requires calling this
    // unconditionally either way.
    const ownSession = useWorkflowGenChatSession({ workflowId, enabled: externalSession == null });
    const {
        session,
        thread,
        creatingSession,
        sendingMessage,
        confirming,
        hasPendingAction,
        sendMessage,
        confirmPendingAction,
    } = externalSession ?? ownSession;

    const busy = sendingMessage || confirming;

    // Surfaced so the editor header can show a working indicator while the
    // panel is collapsed — the session state lives in here, not up there.
    useEffect(() => {
        onBusyChange?.(busy);
    }, [busy, onBusyChange]);

    const runtime = useExternalStoreRuntime<WorkflowGenThreadItem>({
        messages: thread,
        convertMessage,
        isRunning: busy,
        isDisabled: hasPendingAction || busy,
        onNew: async (message) => {
            const textPart = message.content.find((part) => part.type === "text");
            if (textPart && textPart.type === "text") {
                await sendMessage(textPart.text);
            }
        },
    });

    const isEditingExistingWorkflow = workflowId != null;

    const composerPlaceholder = useMemo(() => {
        if (hasPendingAction) return "Resolve the pending action above to continue…";
        return isEditingExistingWorkflow
            ? "Ask for a change to this workflow…"
            : "Describe the agent you want to build…";
    }, [hasPendingAction, isEditingExistingWorkflow]);

    const suggestions = isEditingExistingWorkflow ? EDIT_SUGGESTIONS : BUILD_SUGGESTIONS;
    const isEmpty = thread.length === 0;

    return (
        <div className={cn("flex h-full flex-col bg-background", className)}>
            <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
                <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm font-medium">Scout</span>
                    <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                        v1 beta
                    </span>
                    {busy ? <AssistantWave className="ml-1 text-muted-foreground" /> : null}
                </div>
                {onClose ? (
                    <Button size="icon" variant="ghost" className="h-7 w-7 text-muted-foreground hover:text-foreground" onClick={onClose}>
                        <X className="h-4 w-4" />
                    </Button>
                ) : null}
            </div>

            {!session || creatingSession ? (
                <div className="flex flex-1 items-center justify-center">
                    <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                </div>
            ) : (
                <AssistantRuntimeProvider runtime={runtime}>
                    <WorkflowGenActionsProvider confirming={confirming} onConfirm={confirmPendingAction}>
                        {isEmpty ? (
                            <div className="flex flex-1 flex-col items-center justify-center gap-5 overflow-y-auto px-6 py-8 text-center">
                                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                                    <Sparkles className="h-5 w-5" />
                                </div>
                                <p className="max-w-sm text-sm text-muted-foreground">
                                    {isEditingExistingWorkflow
                                        ? "Ask for a change to this workflow — adding a tool, editing a prompt, reshaping the flow. I'll show you the change for review before anything is saved."
                                        : "Describe the voice agent you want, and I'll draft it here for your review before anything is built."}
                                </p>

                                <div className="w-full max-w-md">
                                    <ChatComposer placeholder={composerPlaceholder} sendingMessage={sendingMessage} />
                                </div>

                                <div className="flex max-w-md flex-wrap items-center justify-center gap-2">
                                    {suggestions.map((suggestion) => (
                                        <button
                                            key={suggestion}
                                            type="button"
                                            onClick={() => {
                                                if (!busy) void sendMessage(suggestion);
                                            }}
                                            className="rounded-full border border-border px-3.5 py-1.5 text-xs text-foreground/80 transition-colors hover:border-foreground/30 hover:bg-muted"
                                        >
                                            {suggestion}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <>
                                <WorkflowGenThreadView />
                                <div className="shrink-0 border-t border-border p-3">
                                    <ChatComposer placeholder={composerPlaceholder} sendingMessage={sendingMessage} />
                                </div>
                            </>
                        )}
                    </WorkflowGenActionsProvider>
                </AssistantRuntimeProvider>
            )}
        </div>
    );
}
