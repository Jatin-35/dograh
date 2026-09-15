"use client";

import {
    AlertTriangle,
    BookOpen,
    ChevronDown,
    ChevronRight,
    Loader2,
    Play,
    X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useAppConfig } from "@/context/AppConfigContext";
import {
    buildSampleEventPayload,
    CodeEditorError,
    parseFunctionSchema,
    type RunOutcome,
    testRun,
    testRunDeployed,
} from "@/lib/codeEditor";
import { resolveWebhookBaseUrl } from "@/lib/webhookUrl";

type Tab = "latest" | "deployed" | "docs";

interface RunTabState {
    payload: string;
    result: RunOutcome | null;
    running: boolean;
    /** Set once, so a later edit to the schema never clobbers what the user
     * typed — same rule the code editor itself uses for unsaved drafts. */
    initialized: boolean;
}

interface PerFileState {
    tab: Tab;
    latest: RunTabState;
    deployed: RunTabState;
}

const EMPTY_RUN_STATE: RunTabState = {
    payload: "",
    result: null,
    running: false,
    initialized: false,
};

function newFileState(): PerFileState {
    return {
        tab: "latest",
        latest: { ...EMPTY_RUN_STATE },
        deployed: { ...EMPTY_RUN_STATE },
    };
}

const TABS: { id: Tab; label: string }[] = [
    { id: "latest", label: "Test Latest" },
    { id: "deployed", label: "Test Deployed" },
    { id: "docs", label: "Docs" },
];

function statusBadge(outcome: RunOutcome) {
    const ok = outcome.statusCode >= 200 && outcome.statusCode < 300;
    return (
        <span
            className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                ok ? "bg-green-100 text-green-800" : "bg-red-100 text-red-800"
            }`}
        >
            {outcome.statusCode}
        </span>
    );
}

/** One of the two always-present, always-separate result sections. Never
 * merges CONSOLE and RETURN VALUE — that distinction is the point: print()
 * output and the function's actual return value answer different questions
 * when you're debugging. */
function ResultSection({
    title,
    content,
    placeholder,
}: {
    title: string;
    content: string | null;
    placeholder: string;
}) {
    const [open, setOpen] = useState(true);
    return (
        <div className="rounded border">
            <button
                type="button"
                className="flex w-full items-center gap-1 px-2 py-1 text-left text-xs font-medium uppercase text-muted-foreground hover:text-foreground"
                onClick={() => setOpen((o) => !o)}
            >
                {open ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                )}
                {title}
            </button>
            {open && (
                <div className="max-h-48 overflow-y-auto border-t px-2 py-2">
                    {content ? (
                        <pre className="whitespace-pre-wrap break-all font-mono text-xs">
                            {content}
                        </pre>
                    ) : (
                        <p className="text-xs text-muted-foreground">{placeholder}</p>
                    )}
                </div>
            )}
        </div>
    );
}

function RunPanel({
    mode,
    schema,
    state,
    onChange,
    dirtyCount,
}: {
    mode: "latest" | "deployed";
    schema: ReturnType<typeof parseFunctionSchema>;
    state: RunTabState;
    onChange: (update: (current: RunTabState) => RunTabState) => void;
    dirtyCount: number;
}) {
    const run = async () => {
        let event: Record<string, unknown>;
        try {
            event = JSON.parse(state.payload);
        } catch {
            toast.error("The event payload is not valid JSON.");
            return;
        }
        // Every write below updates from whatever the state is *now*, never
        // from the `state` this closure captured when Run was pressed. A run
        // takes seconds and the payload box stays editable throughout; writing
        // back a pre-await snapshot silently reverted anything typed while the
        // run was in flight.
        onChange((current) => ({ ...current, running: true }));
        try {
            const outcome =
                mode === "latest" ? await testRun(event) : await testRunDeployed(event);
            onChange((current) => ({ ...current, running: false, result: outcome }));
        } catch (error) {
            onChange((current) => ({ ...current, running: false }));
            if (error instanceof CodeEditorError) {
                toast.error(error.message);
            } else {
                toast.error(error instanceof Error ? error.message : "The run failed");
            }
        }
    };

    const returnValueContent = (() => {
        if (!state.result) return null;
        const parts: string[] = [];
        if (state.result.error) parts.push(`Error: ${state.result.error}`);
        if (state.result.result !== undefined && state.result.result !== null) {
            parts.push(JSON.stringify(state.result.result, null, 2));
        }
        if (state.result.traceback) {
            parts.push(`\nTraceback:\n${state.result.traceback}`);
        }
        return parts.length ? parts.join("\n") : null;
    })();

    return (
        <div className="flex min-h-0 min-w-0 flex-1 gap-3 overflow-hidden p-3">
            <div className="flex w-56 shrink-0 flex-col xl:w-72">
                <div className="mb-1 text-xs font-medium uppercase text-muted-foreground">
                    Event JSON
                </div>
                <Textarea
                    value={state.payload}
                    onChange={(e) =>
                        onChange((current) => ({ ...current, payload: e.target.value }))
                    }
                    className="min-h-0 flex-1 resize-none font-mono text-xs"
                    spellCheck={false}
                />
                {mode === "latest" && dirtyCount > 0 && (
                    <p className="mt-1 flex items-center gap-1 text-xs text-amber-600">
                        <AlertTriangle className="h-3 w-3 shrink-0" />
                        Save first — Test Latest executes what&apos;s stored, not the
                        editor.
                    </p>
                )}
                <Button
                    size="sm"
                    className="mt-2"
                    onClick={run}
                    disabled={state.running || !schema}
                >
                    {state.running ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                        <Play className="mr-2 h-4 w-4" />
                    )}
                    {mode === "latest" ? "Run Latest" : "Run Deployed"}
                </Button>
            </div>

            <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-y-auto text-xs">
                {state.result && (
                    <div className="flex items-center gap-2">
                        <span className="rounded-full border px-2 py-0.5 text-xs text-muted-foreground">
                            Runs: $LATEST
                        </span>
                        {statusBadge(state.result)}
                        {state.result.durationMs !== undefined && (
                            <span className="text-muted-foreground">
                                {state.result.durationMs} ms
                            </span>
                        )}
                        <button
                            type="button"
                            className="ml-auto text-muted-foreground hover:text-foreground"
                            title="Dismiss this result"
                            onClick={() => onChange((current) => ({ ...current, result: null }))}
                        >
                            <X className="h-3.5 w-3.5" />
                        </button>
                    </div>
                )}
                <ResultSection
                    title="Console"
                    content={state.result?.logs || null}
                    placeholder="Run to see output"
                />
                <ResultSection
                    title="Return value"
                    content={returnValueContent}
                    placeholder="Run to see output"
                />
            </div>
        </div>
    );
}

function DocsTab({ schema }: { schema: ReturnType<typeof parseFunctionSchema> }) {
    const { config: appConfig } = useAppConfig();
    const baseUrl = resolveWebhookBaseUrl(appConfig?.tunnelUrl);
    const functionName = schema?.name ?? "your_function";
    const endpoint = `${baseUrl}/api/v1/code-editor/run/${functionName}`;

    const exampleBody: Record<string, unknown> = {};
    for (const param of schema?.parameters ?? []) {
        exampleBody[param.name] = param.example;
    }
    const contextExample = {
        organization_id: 42,
        workflow_id: 7,
        workflow_run_id: 123,
        caller_number: null,
    };

    return (
        <div className="min-h-0 flex-1 overflow-y-auto p-3 text-xs">
            <div className="mx-auto max-w-2xl space-y-5">
                <section>
                    <h3 className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                        How to run your code
                    </h3>
                    <p className="mb-2 text-muted-foreground">
                        Call this deployed function from anywhere with an HTTP POST. The
                        model calls it the same way when your agent uses this tool during
                        a live call.
                    </p>
                    <pre className="overflow-x-auto rounded bg-muted p-2 font-mono">
                        {JSON.stringify(
                            { function_name: functionName, ...exampleBody },
                            null,
                            2,
                        )}
                    </pre>
                </section>

                <section>
                    <h3 className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                        API endpoint
                    </h3>
                    <pre className="overflow-x-auto rounded bg-muted p-2 font-mono">
                        POST {endpoint}
                    </pre>
                </section>

                <section>
                    <h3 className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                        Authentication
                    </h3>
                    <p className="mb-2 text-muted-foreground">
                        Send your organization&apos;s API key as a header — not
                        Authorization: Bearer.
                    </p>
                    <pre className="overflow-x-auto rounded bg-muted p-2 font-mono">
                        X-API-Key: &lt;your-api-key&gt;
                    </pre>
                    <p className="mt-1 text-muted-foreground">
                        Generate one from the{" "}
                        <a href="/api-keys" className="underline">
                            API Keys
                        </a>{" "}
                        page (Developer Portal).
                    </p>
                </section>

                <section>
                    <h3 className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                        What happens automatically
                    </h3>
                    <p className="mb-2 text-muted-foreground">
                        Every call to your router receives a second argument,{" "}
                        <code className="rounded bg-muted px-1">context</code>, carrying
                        which agent and which call triggered it — not a &quot;business&quot;
                        object. Dograh has no such entity; this is the real equivalent.
                    </p>
                    <pre className="overflow-x-auto rounded bg-muted p-2 font-mono">
                        {JSON.stringify(contextExample, null, 2)}
                    </pre>
                    <p className="mt-1 text-muted-foreground">
                        <code className="rounded bg-muted px-1">caller_number</code> is
                        not populated yet on every telephony provider — treat it as
                        possibly null until that lands.{" "}
                        <code className="rounded bg-muted px-1">workflow_id</code> and{" "}
                        <code className="rounded bg-muted px-1">workflow_run_id</code> are
                        only set on a real call — an interactive Test Latest/Test Deployed
                        run here always sees them as null, since there is no call behind
                        it.
                    </p>
                </section>
            </div>
        </div>
    );
}

export function FunctionTestPanel({
    activePath,
    schemaContent,
    dirtyCount,
}: {
    activePath: string;
    schemaContent: string;
    dirtyCount: number;
}) {
    const schema = useMemo(
        () => parseFunctionSchema(activePath, schemaContent),
        [activePath, schemaContent],
    );

    // Keyed by path so switching files never shows another function's payload
    // or results — see the panel's own design note on per-file state.
    const filesRef = useRef<Record<string, PerFileState>>({});
    const [, forceRender] = useState(0);
    const rerender = () => forceRender((n) => n + 1);

    if (!filesRef.current[activePath]) {
        filesRef.current[activePath] = newFileState();
    }
    const fileState = filesRef.current[activePath];

    // First visit to this file: seed the payload from its own schema. Never
    // again after that — a later schema edit must not silently overwrite
    // what the user typed into the payload box.
    useEffect(() => {
        // latest/deployed are always seeded together, so checking one is enough.
        if (fileState.latest.initialized || !schema) return;
        const sample = buildSampleEventPayload(schema);
        fileState.latest = { ...fileState.latest, payload: sample, initialized: true };
        fileState.deployed = {
            ...fileState.deployed,
            payload: sample,
            initialized: true,
        };
        rerender();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activePath, schema]);

    const setTab = (tab: Tab) => {
        fileState.tab = tab;
        rerender();
    };

    return (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="flex items-center gap-1 border-b px-2">
                {TABS.map((t) => (
                    <button
                        key={t.id}
                        type="button"
                        className={`flex items-center gap-1 border-b-2 px-2 py-1.5 text-xs font-medium ${
                            fileState.tab === t.id
                                ? "border-primary text-foreground"
                                : "border-transparent text-muted-foreground hover:text-foreground"
                        }`}
                        onClick={() => setTab(t.id)}
                    >
                        {t.id === "docs" && <BookOpen className="h-3.5 w-3.5" />}
                        {t.label}
                    </button>
                ))}
            </div>

            {fileState.tab === "docs" ? (
                <DocsTab schema={schema} />
            ) : (
                (() => {
                    // Frozen here, not re-read inside onChange: a run started on
                    // this tab must write its result back into *this* tab's slot
                    // even if the user has since switched tabs (or files —
                    // `fileState` itself is already a stable per-file reference)
                    // while the request was in flight. Reading `fileState.tab`
                    // live inside onChange would let a slow "Run Latest" land its
                    // result under "Test Deployed" if the user switched away
                    // before it finished.
                    const mode = fileState.tab;
                    return (
                        <RunPanel
                            mode={mode}
                            schema={schema}
                            dirtyCount={dirtyCount}
                            state={fileState[mode]}
                            onChange={(update) => {
                                fileState[mode] = update(fileState[mode]);
                                rerender();
                            }}
                        />
                    );
                })()
            )}
        </div>
    );
}
