// ---------------------------------------------------------------------------
// Shared types for the in-product AI assistant chat (standalone builder +
// per-workflow editor panel). Mirrors the frozen backend SSE contract in
// api/services/workflow_gen/agent_loop.py exactly — keep the two in sync.
// ---------------------------------------------------------------------------

export type WorkflowGenErrorCode = "llm_failure" | "tool_failure" | "validation_failed" | "internal";

export type WorkflowGenEvent =
    | { type: "status"; data: { message: string } }
    | { type: "assistant"; data: { message: string } }
    | {
        type: "approval";
        data: {
            action_id: string;
            action_type: "create_workflow" | "save_workflow" | "create_tool" | "create_credential";
            summary: string;
            definition_preview: Record<string, unknown>;
        };
    }
    | {
        type: "workflow_ready";
        data: {
            workflow_id: number;
            name: string;
            node_count: number;
            edge_count: number;
            valid: boolean;
            url: string;
        };
    }
    | { type: "error"; data: { code: WorkflowGenErrorCode; message: string } }
    | { type: "done"; data: Record<string, never> };

export type WorkflowGenSseFrame = WorkflowGenEvent & { revision: number };

/** Anything renderable in the thread. */
export type WorkflowGenThreadItem =
    | { id: string; kind: "user"; text: string }
    | { id: string; kind: "assistant"; text: string }
    | {
        id: string;
        kind: "approval";
        actionId: string;
        actionType: "create_workflow" | "save_workflow" | "create_tool" | "create_credential";
        summary: string;
        definitionPreview: Record<string, unknown>;
        resolved: boolean;
    }
    | {
        id: string;
        kind: "workflow_ready";
        workflowId: number;
        name: string;
        nodeCount: number;
        edgeCount: number;
        url: string;
    }
    | { id: string; kind: "error"; code: WorkflowGenErrorCode; text: string }
    /** The work done during one turn, accumulated in order. Status events used
     * to overwrite a single line above the composer and vanish at the end of
     * the turn, so there was no way to see what the assistant actually did —
     * these keep that history in the thread instead. `running` is false once
     * the turn produces its reply. */
    | { id: string; kind: "steps"; steps: string[]; running: boolean };

// Raw persisted message shapes (OpenAI chat-completions format) — only used
// to reconstruct `user`/`assistant` thread items from a session's stored
// `messages` array on load (GET /workflow-gen/sessions/{id}); everything
// else in the thread comes from live SSE events, not from re-parsing
// tool-call machinery out of the transcript.
export interface WorkflowGenRawMessage {
    role: "user" | "assistant" | "tool" | "system";
    content?: string | null;
}
