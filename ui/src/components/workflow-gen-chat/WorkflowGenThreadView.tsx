"use client";

import { MessagePrimitive, type TextMessagePartProps, ThreadPrimitive, type ToolCallMessagePartProps } from "@assistant-ui/react";
import { ArrowRight, CheckCircle2, ChevronDown, Sparkles, Wand2, Workflow, Wrench, XCircle } from "lucide-react";
import { createContext, useContext } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { AssistantWave } from "./AssistantWave";
import type { WorkflowGenErrorCode, WorkflowGenThreadItem } from "./types";

const ENTRANCE = "animate-in fade-in-0 slide-in-from-bottom-2 duration-300";

// ---------------------------------------------------------------------------
// Confirm/cancel plumbing for the approval tool-call card. The tool renderer
// components below are registered once with assistant-ui and don't receive
// props directly from WorkflowGenChatPanel, so `confirming`/`onConfirm` (the
// real handlers owned by useWorkflowGenChatSession) are threaded through
// context instead.
// ---------------------------------------------------------------------------
interface WorkflowGenActionsContextValue {
    confirming: boolean;
    onConfirm: (actionId: string, approve: boolean) => void;
}

const WorkflowGenActionsContext = createContext<WorkflowGenActionsContextValue | null>(null);

export function WorkflowGenActionsProvider({
    confirming,
    onConfirm,
    children,
}: WorkflowGenActionsContextValue & { children: React.ReactNode }) {
    return (
        <WorkflowGenActionsContext.Provider value={{ confirming, onConfirm }}>
            {children}
        </WorkflowGenActionsContext.Provider>
    );
}

function useWorkflowGenActions(): WorkflowGenActionsContextValue {
    const ctx = useContext(WorkflowGenActionsContext);
    if (!ctx) {
        throw new Error("useWorkflowGenActions must be used within a WorkflowGenActionsProvider");
    }
    return ctx;
}

// ---------------------------------------------------------------------------
// Tool-call args shapes — mirror the `approval`/`workflow_ready`/`error`
// thread-item kinds in ./types.ts, since each becomes a single tool-call
// content part on an assistant message (see WorkflowGenChatPanel's
// `convertMessage`).
// ---------------------------------------------------------------------------
type ApprovalItem = Extract<WorkflowGenThreadItem, { kind: "approval" }>;
interface ReviewActionArgs {
    actionId: ApprovalItem["actionId"];
    actionType: ApprovalItem["actionType"];
    summary: ApprovalItem["summary"];
    definitionPreview: ApprovalItem["definitionPreview"];
}

interface WorkflowReadyArgs {
    workflowId: number;
    name: string;
    nodeCount: number;
    edgeCount: number;
    url: string;
}

interface ReportErrorArgs {
    code: WorkflowGenErrorCode;
    text: string;
}

function Avatar() {
    return (
        <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
            <Sparkles className="h-3.5 w-3.5" />
        </div>
    );
}

/** Plain (non-markdown) text for user bubbles. */
export function UserText({ text }: TextMessagePartProps) {
    return <>{text}</>;
}

/** Assistant text, rendered through the existing tested markdown treatment. */
export function AssistantMarkdownText({ text }: TextMessagePartProps) {
    return (
        <div className={cn("flex items-start gap-2", ENTRANCE)}>
            <Avatar />
            <div className="markdown-sm max-w-[85%] rounded-2xl rounded-tl-md bg-muted px-3.5 py-2.5 text-sm text-foreground">
                <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                        p: ({ children }) => <p className="mb-2 leading-relaxed last:mb-0">{children}</p>,
                        ul: ({ children }) => <ul className="mb-2 ml-4 list-disc space-y-0.5 last:mb-0">{children}</ul>,
                        ol: ({ children }) => <ol className="mb-2 ml-4 list-decimal space-y-0.5 last:mb-0">{children}</ol>,
                        li: ({ children }) => <li className="leading-relaxed">{children}</li>,
                        strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
                        a: ({ children, href }) => (
                            <a href={href} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-2">
                                {children}
                            </a>
                        ),
                        code: ({ children }) => (
                            <code className="rounded bg-foreground/10 px-1 py-0.5 font-mono text-xs">{children}</code>
                        ),
                        pre: ({ children }) => (
                            <pre className="mb-2 overflow-x-auto rounded-lg bg-foreground/10 p-2.5 font-mono text-xs last:mb-0">
                                {children}
                            </pre>
                        ),
                    }}
                >
                    {text}
                </ReactMarkdown>
            </div>
        </div>
    );
}

function summarizeDefinition(actionType: string, preview: Record<string, unknown>) {
    if (actionType === "create_tool") {
        const toolDefinition = preview.tool_definition as Record<string, unknown> | undefined;
        const definition = toolDefinition?.definition as Record<string, unknown> | undefined;
        return {
            icon: Wrench,
            title: (toolDefinition?.name as string) || "New tool",
            chips: definition?.type ? [String(definition.type)] : [],
        };
    }
    // Counts come from the server having already parsed the proposed source,
    // so they reflect what will actually be built.
    const nodeCount = typeof preview.node_count === "number" ? preview.node_count : undefined;
    const edgeCount = typeof preview.edge_count === "number" ? preview.edge_count : 0;
    return {
        icon: actionType === "create_workflow" ? Workflow : Wand2,
        title: (preview.name as string) || (actionType === "create_workflow" ? "New workflow" : "Workflow update"),
        chips: [] as string[],
        nodeCount,
        edgeCount,
    };
}

/** Renders the `reviewAction` tool call as the approval card. */
export function ReviewActionToolUI({ args, result }: ToolCallMessagePartProps<ReviewActionArgs, boolean>) {
    const { confirming, onConfirm } = useWorkflowGenActions();
    const resolved = Boolean(result);
    const { icon: Icon, title, chips, nodeCount, edgeCount } = summarizeDefinition(args.actionType, args.definitionPreview);
    // Workflow actions carry SDK TypeScript — far more readable than the
    // surrounding JSON envelope, so show it directly when present.
    const rawCode = args.definitionPreview?.code;
    const sourceCode = typeof rawCode === "string" ? rawCode : null;
    // A plain-language summary of the edit, computed server-side against the
    // stored workflow — far more useful for deciding than reading the source.
    const rawChanges = args.definitionPreview?.changes;
    const changes = Array.isArray(rawChanges) ? (rawChanges as string[]) : [];

    return (
        <div className={cn("overflow-hidden rounded-xl border border-border bg-card shadow-sm", ENTRANCE)}>
            <div className="flex items-start gap-3 border-b border-border/70 bg-muted/40 px-4 py-3">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-amber-500/15 text-amber-600 dark:text-amber-400">
                    <Icon className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-foreground">{title}</p>
                    <p className="text-xs text-muted-foreground">{args.summary}</p>
                </div>
            </div>

            {(chips.length > 0 || nodeCount !== undefined) && (
                <div className="flex flex-wrap items-center gap-1.5 px-4 py-3">
                    {nodeCount !== undefined && (
                        <span className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium text-muted-foreground">
                            {nodeCount} node{nodeCount === 1 ? "" : "s"} · {edgeCount} edge{edgeCount === 1 ? "" : "s"}
                        </span>
                    )}
                    {chips.map((chip) => (
                        <span key={chip} className="rounded-full border border-border px-2.5 py-1 text-xs text-foreground/80">
                            {chip}
                        </span>
                    ))}
                </div>
            )}

            {changes.length > 0 ? (
                <ul className="space-y-1 border-t border-border/70 px-4 py-3 text-sm text-foreground/90">
                    {changes.map((change, i) => (
                        <li key={i} className="flex items-start gap-2">
                            <span className="mt-[0.45rem] h-1 w-1 shrink-0 rounded-full bg-muted-foreground" />
                            <span>{change}</span>
                        </li>
                    ))}
                </ul>
            ) : null}

            <details className="group border-t border-border/70 px-4 py-2">
                <summary className="flex cursor-pointer select-none items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                    <ChevronDown className="h-3 w-3 transition-transform group-open:rotate-180" />
                    {sourceCode ? "View source" : "View raw definition"}
                </summary>
                <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-foreground/5 p-2.5 font-mono text-[11px] leading-relaxed">
                    {sourceCode ?? JSON.stringify(args.definitionPreview, null, 2)}
                </pre>
            </details>

            <div className="flex items-center gap-2 border-t border-border/70 px-4 py-3">
                {!resolved ? (
                    <>
                        <Button size="sm" disabled={confirming} onClick={() => onConfirm(args.actionId, true)}>
                            Confirm & Build
                        </Button>
                        <Button size="sm" variant="outline" disabled={confirming} onClick={() => onConfirm(args.actionId, false)}>
                            Cancel
                        </Button>
                    </>
                ) : (
                    <p className="text-xs text-muted-foreground">Resolved.</p>
                )}
            </div>
        </div>
    );
}

/** Renders the `workflowReady` tool call as the success card. */
export function WorkflowReadyToolUI({ args }: ToolCallMessagePartProps<WorkflowReadyArgs, boolean>) {
    return (
        <div className={cn("flex items-start gap-3 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4", ENTRANCE)}>
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-foreground">{args.name}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                    {args.nodeCount} node{args.nodeCount === 1 ? "" : "s"} · {args.edgeCount} edge{args.edgeCount === 1 ? "" : "s"}
                </p>
                <Button size="sm" variant="outline" className="mt-3" asChild>
                    <a href={args.url}>
                        Open workflow
                        <ArrowRight className="h-3.5 w-3.5" />
                    </a>
                </Button>
            </div>
        </div>
    );
}

/** Renders the `reportError` tool call as the error card. */
export function ReportErrorToolUI({ args }: ToolCallMessagePartProps<ReportErrorArgs, boolean>) {
    return (
        <div className={cn("flex items-start gap-3 rounded-xl border border-destructive/20 bg-destructive/5 p-4", ENTRANCE)}>
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-destructive/15 text-destructive">
                <XCircle className="h-4 w-4" />
            </div>
            <p className="pt-1 text-sm text-foreground/90">{args.text}</p>
        </div>
    );
}

interface ActivityStepsArgs {
    steps: string[];
    running: boolean;
}

/** Renders one turn's work as a compact, expandable activity log.
 *
 * Collapsed it shows the current step while running, or a one-line summary
 * once finished — so a turn's history stays readable without burying the
 * conversation in a step per line. */
export function ActivityStepsToolUI({ args }: ToolCallMessagePartProps<ActivityStepsArgs, boolean>) {
    const steps = args.steps ?? [];
    if (steps.length === 0) return null;
    const current = steps[steps.length - 1];

    return (
        <details className={cn("group rounded-lg border border-border/70 bg-muted/30", ENTRANCE)} open={args.running}>
            <summary className="flex cursor-pointer select-none items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground">
                {args.running ? (
                    <AssistantWave className="h-3 shrink-0" />
                ) : (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                )}
                <span className="min-w-0 flex-1 truncate">
                    {args.running ? current : `${steps.length} step${steps.length === 1 ? "" : "s"}`}
                </span>
                <ChevronDown className="h-3 w-3 shrink-0 transition-transform group-open:rotate-180" />
            </summary>
            <ol className="space-y-1.5 px-3 pb-2.5 pl-8 text-xs text-muted-foreground">
                {steps.map((step, i) => {
                    const isCurrent = args.running && i === steps.length - 1;
                    return (
                        <li key={`${step}-${i}`} className="flex items-center gap-2">
                            {isCurrent ? (
                                <AssistantWave className="h-2.5 shrink-0" />
                            ) : (
                                <CheckCircle2 className="h-3 w-3 shrink-0 opacity-60" />
                            )}
                            <span className={cn(isCurrent && "text-foreground")}>{step}</span>
                        </li>
                    );
                })}
            </ol>
        </details>
    );
}

function UserMessage() {
    return (
        <MessagePrimitive.Root className={cn("flex justify-end", ENTRANCE)}>
            <div className="max-w-[85%] rounded-2xl rounded-br-md bg-primary px-3.5 py-2.5 text-sm text-primary-foreground">
                <MessagePrimitive.Content components={{ Text: UserText }} unstable_showEmptyOnNonTextEnd={false} />
            </div>
        </MessagePrimitive.Root>
    );
}

function AssistantMessage() {
    return (
        <MessagePrimitive.Root>
            <MessagePrimitive.Content
                unstable_showEmptyOnNonTextEnd={false}
                components={{
                    Text: AssistantMarkdownText,
                    tools: {
                        by_name: {
                            reviewAction: ReviewActionToolUI,
                            workflowReady: WorkflowReadyToolUI,
                            reportError: ReportErrorToolUI,
                            activitySteps: ActivityStepsToolUI,
                        },
                        Fallback: () => null,
                    },
                }}
            />
        </MessagePrimitive.Root>
    );
}

interface WorkflowGenThreadViewProps {
    className?: string;
}

export function WorkflowGenThreadView({ className }: WorkflowGenThreadViewProps) {
    return (
        <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
            <ThreadPrimitive.Viewport className={cn("flex-1 space-y-4 overflow-y-auto p-4", className)}>
                <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
            </ThreadPrimitive.Viewport>
        </ThreadPrimitive.Root>
    );
}
