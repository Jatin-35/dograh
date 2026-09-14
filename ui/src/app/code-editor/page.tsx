"use client";

import Editor from "@monaco-editor/react";
import {
    AlertTriangle,
    ChevronDown,
    ChevronRight,
    FileCode,
    FileJson,
    FileText,
    Loader2,
    PanelLeftClose,
    PanelLeftOpen,
    Play,
    Plus,
    Rocket,
    Save,
    Settings,
    Sparkles,
    Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { ResizeHandle } from "@/components/code-editor/ResizeHandle";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { AssistantWave } from "@/components/workflow-gen-chat/AssistantWave";
import { useScoutEnabled } from "@/components/workflow-gen-chat/useScoutEnabled";
import { WorkflowGenChatPanel } from "@/components/workflow-gen-chat/WorkflowGenChatPanel";
import { useAuth } from "@/lib/auth";
import {
    CodeEditorError,
    type CodeFile,
    type CodeVersion,
    createVersion,
    deleteEnvVar,
    deleteFile,
    deployVersion,
    type EnvVar,
    groupByFolder,
    languageFor,
    listEnvVars,
    listFiles,
    listVersions,
    type RunOutcome,
    saveFile,
    setEnvVar,
    testRun,
} from "@/lib/codeEditor";

const ENTRY_POINT = "all_events_entry_point.py";
const LAYOUT_KEY = "dograh:code-editor:layout";

const clampWidth = (value: number, min: number, max: number) =>
    Math.min(Math.max(value, min), max);

const FOLDER_LABELS: Record<string, string> = {
    "": "",
    function_definitions: "function_definitions",
    agents: "agents",
};

function iconFor(path: string) {
    if (path.endsWith(".json")) return FileJson;
    if (path.endsWith(".py")) return FileCode;
    return FileText;
}

export default function CodeEditorPage() {
    const { user, loading: authLoading } = useAuth();
    const { enabled: scoutEnabled } = useScoutEnabled();
    const [scoutOpen, setScoutOpen] = useState(false);
    // Mirrors the workflow editor's button: while a turn is in flight the
    // icon becomes a waveform, so you can tell Scout is working even with the
    // panel closed.
    const [scoutBusy, setScoutBusy] = useState(false);
    const [filesOpen, setFilesOpen] = useState(true);
    const [testOpen, setTestOpen] = useState(true);
    const [filesWidth, setFilesWidth] = useState(224);
    const [scoutWidth, setScoutWidth] = useState(360);
    const [testHeight, setTestHeight] = useState(240);

    const [files, setFiles] = useState<CodeFile[]>([]);
    const [activePath, setActivePath] = useState<string>(ENTRY_POINT);
    const [drafts, setDrafts] = useState<Record<string, string>>({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [problems, setProblems] = useState<string[]>([]);

    const [running, setRunning] = useState(false);
    const [outcome, setOutcome] = useState<RunOutcome | null>(null);
    const [payload, setPayload] = useState(
        '{\n  "function_name": "get_order_status",\n  "order_id": "ORD-12345"\n}',
    );

    const [versions, setVersions] = useState<CodeVersion[]>([]);
    const [versionsOpen, setVersionsOpen] = useState(false);
    const [versionNote, setVersionNote] = useState("");
    const [busyVersion, setBusyVersion] = useState(false);

    const [envOpen, setEnvOpen] = useState(false);
    const [envVars, setEnvVars] = useState<EnvVar[]>([]);
    const [newKey, setNewKey] = useState("");
    const [newValue, setNewValue] = useState("");

    const [newFileOpen, setNewFileOpen] = useState(false);
    const [newFilePath, setNewFilePath] = useState("function_definitions/");

    const hasFetched = useRef(false);

    useEffect(() => {
        try {
            const saved = window.localStorage.getItem(LAYOUT_KEY);
            if (!saved) return;
            const { files, scout, test } = JSON.parse(saved);
            if (typeof files === "number") setFilesWidth(clampWidth(files, 160, 480));
            if (typeof scout === "number") setScoutWidth(clampWidth(scout, 280, 640));
            if (typeof test === "number") setTestHeight(clampWidth(test, 120, 520));
        } catch {
            // A corrupt or unreadable entry just means default sizes.
        }
    }, []);

    useEffect(() => {
        try {
            window.localStorage.setItem(
                LAYOUT_KEY,
                JSON.stringify({ files: filesWidth, scout: scoutWidth, test: testHeight }),
            );
        } catch {
            // Private browsing and similar — the layout simply won't persist.
        }
    }, [filesWidth, scoutWidth, testHeight]);

    const activeContent = useMemo(() => {
        if (activePath in drafts) return drafts[activePath];
        return files.find((f) => f.path === activePath)?.content ?? "";
    }, [activePath, drafts, files]);

    const isDirty = activePath in drafts;
    const dirtyCount = Object.keys(drafts).length;

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const loaded = await listFiles();
            setFiles(loaded);
            if (!loaded.some((f) => f.path === activePath)) {
                setActivePath(loaded[0]?.path ?? ENTRY_POINT);
            }
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load");
        } finally {
            setLoading(false);
        }
    }, [activePath]);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        void load();
    }, [authLoading, user, load]);

    // A browser tab close is the one way unsaved work here is genuinely lost —
    // there is no local copy of a virtual file.
    useEffect(() => {
        if (dirtyCount === 0) return;
        const warn = (event: BeforeUnloadEvent) => event.preventDefault();
        window.addEventListener("beforeunload", warn);
        return () => window.removeEventListener("beforeunload", warn);
    }, [dirtyCount]);


    /** Re-read the workspace without disturbing unsaved work.
     *
     * Scout writes files server-side, so the page has to notice. A plain refetch
     * would be wrong: anything with a local draft is mid-edit, and replacing it
     * would silently discard typing. So stored content is updated everywhere,
     * but a file the user has touched keeps showing their version until they
     * save or discard it.
     */
    const syncFromServer = useCallback(async () => {
        let loaded: CodeFile[];
        try {
            loaded = await listFiles();
        } catch {
            return; // A failed background sync should stay invisible.
        }

        setFiles((previous) => {
            const before = new Set(previous.map((file) => file.path));
            const added = loaded.filter((file) => !before.has(file.path));
            if (added.length > 0) {
                toast.success(
                    added.length === 1
                        ? `Scout added ${added[0].path}`
                        : `Scout added ${added.length} files`,
                );
            }
            return loaded;
        });

        setDrafts((previous) => {
            // Drop drafts whose file is gone, and any whose content now matches
            // what was saved — that draft is no longer a pending change.
            const next: Record<string, string> = {};
            for (const [path, content] of Object.entries(previous)) {
                const stored = loaded.find((file) => file.path === path);
                if (stored && stored.content !== content) next[path] = content;
            }
            return next;
        });

        setActivePath((current) =>
            loaded.some((file) => file.path === current)
                ? current
                : loaded[0]?.path ?? ENTRY_POINT,
        );
    }, []);

    // Scout finishing a turn is the moment its edits have landed. Watching the
    // busy flag means no polling and no extra plumbing between the panel and
    // this page.
    const scoutWasBusy = useRef(false);
    useEffect(() => {
        if (scoutWasBusy.current && !scoutBusy) void syncFromServer();
        scoutWasBusy.current = scoutBusy;
    }, [scoutBusy, syncFromServer]);

    // Coming back to the tab is the other moment the workspace may have moved
    // on — a second window, or Scout in the workflow editor.
    useEffect(() => {
        const onFocus = () => {
            if (document.visibilityState === "visible") void syncFromServer();
        };
        document.addEventListener("visibilitychange", onFocus);
        return () => document.removeEventListener("visibilitychange", onFocus);
    }, [syncFromServer]);

    const handleSave = useCallback(async () => {
        if (!isDirty) return;
        setSaving(true);
        setProblems([]);
        try {
            const result = await saveFile(activePath, activeContent);
            setFiles((prev) =>
                prev.some((f) => f.path === activePath)
                    ? prev.map((f) =>
                          f.path === activePath ? { ...f, content: activeContent } : f,
                      )
                    : [...prev, { path: activePath, content: activeContent }].sort((a, b) =>
                          a.path.localeCompare(b.path),
                      ),
            );
            setDrafts((prev) => {
                const next = { ...prev };
                delete next[activePath];
                return next;
            });
            if (result.warnings.length) {
                setProblems(result.warnings);
                toast.warning(`Saved with ${result.warnings.length} warning(s).`);
            } else {
                toast.success(`Saved ${activePath}`);
            }
        } catch (error) {
            // Validation problems belong against the file, not in a toast that
            // vanishes before it can be acted on.
            if (error instanceof CodeEditorError && error.errors.length) {
                setProblems(error.errors);
                toast.error("Not saved — see the problems below.");
            } else {
                toast.error(error instanceof Error ? error.message : "Failed to save");
            }
        } finally {
            setSaving(false);
        }
    }, [activePath, activeContent, isDirty]);

    // Ctrl/Cmd+S, because anyone typing in an editor will press it.
    useEffect(() => {
        const onKey = (event: KeyboardEvent) => {
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
                event.preventDefault();
                void handleSave();
            }
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [handleSave]);

    const handleRun = async () => {
        let event: Record<string, unknown>;
        try {
            event = JSON.parse(payload);
        } catch {
            toast.error("The test payload is not valid JSON.");
            return;
        }
        if (dirtyCount > 0) {
            toast.warning("Save first — Test Run executes what's stored, not the editor.");
            return;
        }
        setRunning(true);
        setOutcome(null);
        try {
            setOutcome(await testRun(event));
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "The run failed");
        } finally {
            setRunning(false);
        }
    };

    const openVersions = async () => {
        setVersionsOpen(true);
        try {
            setVersions(await listVersions());
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load versions");
        }
    };

    const handleCreateVersion = async () => {
        setBusyVersion(true);
        try {
            const version = await createVersion(versionNote.trim());
            toast.success(`Created v${version.version_number}`);
            setVersionNote("");
            setVersions(await listVersions());
        } catch (error) {
            if (error instanceof CodeEditorError && error.errors.length) {
                toast.error(`${error.message} ${error.errors[0]}`);
            } else {
                toast.error(error instanceof Error ? error.message : "Failed");
            }
        } finally {
            setBusyVersion(false);
        }
    };

    const handleDeploy = async (versionNumber: number) => {
        setBusyVersion(true);
        try {
            const report = await deployVersion(versionNumber);
            const changed = [
                report.created.length && `${report.created.length} created`,
                report.updated.length && `${report.updated.length} updated`,
                report.archived.length && `${report.archived.length} archived`,
            ].filter(Boolean);
            toast.success(
                `Deployed v${versionNumber}${changed.length ? ` — ${changed.join(", ")}` : ""}`,
            );
            setVersions(await listVersions());
        } catch (error) {
            if (error instanceof CodeEditorError && error.errors.length) {
                toast.error(error.errors[0]);
            } else {
                toast.error(error instanceof Error ? error.message : "Deploy failed");
            }
        } finally {
            setBusyVersion(false);
        }
    };

    const openEnv = async () => {
        setEnvOpen(true);
        try {
            setEnvVars(await listEnvVars());
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load");
        }
    };

    const handleAddEnv = async () => {
        if (!newKey.trim() || !newValue) return;
        try {
            await setEnvVar(newKey.trim(), newValue);
            setNewKey("");
            setNewValue("");
            setEnvVars(await listEnvVars());
            toast.success("Saved. The value is encrypted and won't be shown again.");
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to save");
        }
    };

    const handleDeleteFile = async (path: string) => {
        if (path === ENTRY_POINT) {
            toast.error("The router can't be deleted — everything dispatches through it.");
            return;
        }
        try {
            await deleteFile(path);
            setFiles((prev) => prev.filter((f) => f.path !== path));
            setDrafts((prev) => {
                const next = { ...prev };
                delete next[path];
                return next;
            });
            if (activePath === path) setActivePath(ENTRY_POINT);
            toast.success(`Deleted ${path}`);
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to delete");
        }
    };

    const handleNewFile = () => {
        const path = newFilePath.trim();
        if (!path || files.some((f) => f.path === path)) {
            toast.error(path ? "That file already exists." : "Enter a path.");
            return;
        }
        const starter = path.endsWith(".json")
            ? JSON.stringify(
                  {
                      name: path.split("/").pop()?.replace(".json", ""),
                      description: "",
                      parameters: { type: "object", properties: {}, required: [] },
                  },
                  null,
                  2,
              )
            : "";
        setFiles((prev) => [...prev, { path, content: starter }].sort((a, b) => a.path.localeCompare(b.path)));
        setDrafts((prev) => ({ ...prev, [path]: starter }));
        setActivePath(path);
        setNewFileOpen(false);
        setNewFilePath("function_definitions/");
    };

    const grouped = useMemo(() => groupByFolder(files), [files]);

    return (
        <main className="flex h-[calc(100dvh-3.5rem)] w-full min-w-0 flex-col overflow-hidden">
            <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
                <div>
                    <h1 className="text-lg font-semibold">Code Editor</h1>
                    <p className="text-xs text-muted-foreground">
                        Custom Python functions your agents can call.
                        {dirtyCount > 0 && (
                            <span className="ml-1 font-medium text-amber-600">
                                {dirtyCount} unsaved file(s)
                            </span>
                        )}
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    {scoutEnabled && (
                        <Button
                            variant={scoutOpen ? "secondary" : "outline"}
                            size="sm"
                            className="flex items-center gap-1.5"
                            onClick={() => setScoutOpen((open) => !open)}
                            title={scoutBusy ? "Scout — working…" : "Scout"}
                        >
                            {scoutBusy ? (
                                <AssistantWave />
                            ) : (
                                <Sparkles className="h-4 w-4" />
                            )}
                            Scout
                        </Button>
                    )}
                    <Button variant="outline" size="sm" onClick={openEnv}>
                        <Settings className="mr-2 h-4 w-4" />
                        Variables
                    </Button>
                    <Button variant="outline" size="sm" onClick={openVersions}>
                        <Rocket className="mr-2 h-4 w-4" />
                        Versions &amp; Deploy
                    </Button>
                    <Button size="sm" onClick={handleSave} disabled={!isDirty || saving}>
                        {saving ? (
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                            <Save className="mr-2 h-4 w-4" />
                        )}
                        Save
                    </Button>
                </div>
            </header>

            <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden lg:flex-row">
                {/* File tree */}
                {!filesOpen && (
                    <div className="shrink-0 border-r p-2">
                        <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setFilesOpen(true)}
                            title="Show files"
                        >
                            <PanelLeftOpen className="h-4 w-4" />
                        </Button>
                    </div>
                )}

                <aside
                    className={`w-full shrink-0 overflow-y-auto border-b p-3 lg:border-b-0 ${
                        filesOpen ? "" : "hidden"
                    }`}
                    style={filesOpen ? { width: filesWidth } : undefined}
                >
                    <div className="mb-2 flex items-center justify-between">
                        <span className="text-xs font-medium uppercase text-muted-foreground">
                            Files
                        </span>
                        <div className="flex items-center">
                            <Button variant="ghost" size="sm" onClick={() => setNewFileOpen(true)}>
                                <Plus className="h-4 w-4" />
                            </Button>
                            <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => setFilesOpen(false)}
                                title="Hide files"
                            >
                                <PanelLeftClose className="h-4 w-4" />
                            </Button>
                        </div>
                    </div>

                    {loading ? (
                        <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
                            <Loader2 className="h-4 w-4 animate-spin" />
                            Loading…
                        </div>
                    ) : (
                        [...grouped.entries()].map(([folder, folderFiles]) => (
                            <div key={folder || "root"} className="mb-3">
                                {folder && (
                                    <div className="mb-1 text-xs text-muted-foreground">
                                        {FOLDER_LABELS[folder] ?? folder}/
                                    </div>
                                )}
                                {folderFiles.map((file) => {
                                    const Icon = iconFor(file.path);
                                    const name = folder
                                        ? file.path.slice(folder.length + 1)
                                        : file.path;
                                    return (
                                        <div
                                            key={file.path}
                                            className={`group flex items-center gap-1 rounded px-2 py-1 text-sm ${
                                                activePath === file.path
                                                    ? "bg-accent font-medium"
                                                    : "hover:bg-accent/50"
                                            }`}
                                        >
                                            <button
                                                type="button"
                                                className="flex min-w-0 flex-1 items-center gap-2 text-left"
                                                onClick={() => setActivePath(file.path)}
                                            >
                                                <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                                                <span className="truncate">{name}</span>
                                                {file.path in drafts && (
                                                    <span
                                                        className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500"
                                                        title="Unsaved"
                                                    />
                                                )}
                                            </button>
                                            {file.path !== ENTRY_POINT && (
                                                <button
                                                    type="button"
                                                    className="opacity-0 transition group-hover:opacity-100"
                                                    onClick={() => handleDeleteFile(file.path)}
                                                    title={`Delete ${file.path}`}
                                                >
                                                    <Trash2 className="h-3.5 w-3.5 text-muted-foreground hover:text-destructive" />
                                                </button>
                                            )}
                                        </div>
                                    );
                                })}
                            </div>
                        ))
                    )}
                </aside>

                {filesOpen && (
                    <ResizeHandle
                        orientation="vertical"
                        label="Resize the file list"
                        onDelta={(delta) =>
                            setFilesWidth((width) => clampWidth(width + delta, 160, 480))
                        }
                        onDoubleClick={() => setFilesWidth(224)}
                    />
                )}

                {/* Editor */}
                <section className="flex min-h-0 min-w-0 flex-1 flex-col">
                    <div className="flex items-center justify-between gap-2 border-b px-3 py-1.5 text-xs text-muted-foreground">
                        <span className="truncate font-mono">{activePath}</span>
                        {isDirty && <span className="text-amber-600">unsaved</span>}
                    </div>

                    <div className="min-h-0 flex-1">
                        <Editor
                            height="100%"
                            path={activePath}
                            language={languageFor(activePath)}
                            value={activeContent}
                            onChange={(value) =>
                                setDrafts((prev) => ({ ...prev, [activePath]: value ?? "" }))
                            }
                            options={{
                                minimap: { enabled: false },
                                fontSize: 13,
                                tabSize: 4,
                                scrollBeyondLastLine: false,
                                automaticLayout: true,
                            }}
                        />
                    </div>

                    {problems.length > 0 && (
                        <div className="max-h-32 shrink-0 overflow-y-auto border-t bg-red-50 px-3 py-2 text-xs text-red-700">
                            <div className="mb-1 flex items-center gap-1 font-medium">
                                <AlertTriangle className="h-3.5 w-3.5" />
                                Problems
                            </div>
                            {problems.map((problem) => (
                                <div key={problem} className="font-mono">
                                    {problem}
                                </div>
                            ))}
                        </div>
                    )}
                    {/* Test Run sits under the editor rather than beside it:
                        four side-by-side columns overflowed the viewport, and this
                        is the panel that needs width more than height. */}
                    <>
                        {testOpen && (
                            <ResizeHandle
                                orientation="horizontal"
                                label="Resize the test panel"
                                onDelta={(delta) =>
                                    setTestHeight((height) =>
                                        clampWidth(height - delta, 120, 520),
                                    )
                                }
                                onDoubleClick={() => setTestHeight(240)}
                            />
                        )}
                        <div
                            className="flex shrink-0 flex-col border-t"
                            style={testOpen ? { height: testHeight } : undefined}
                        >
                        <div className="flex items-center gap-2 px-3 py-1.5">
                            <button
                                type="button"
                                className="flex items-center gap-1 text-xs font-medium uppercase text-muted-foreground hover:text-foreground"
                                onClick={() => setTestOpen((open) => !open)}
                            >
                                {testOpen ? (
                                    <ChevronDown className="h-3.5 w-3.5" />
                                ) : (
                                    <ChevronRight className="h-3.5 w-3.5" />
                                )}
                                Test Run
                            </button>
                            {outcome && !testOpen && (
                                <span
                                    className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                                        outcome.statusCode === 200
                                            ? "bg-green-100 text-green-800"
                                            : "bg-red-100 text-red-800"
                                    }`}
                                >
                                    {outcome.statusCode}
                                </span>
                            )}
                            <Button
                                size="sm"
                                className="ml-auto"
                                onClick={handleRun}
                                disabled={running}
                            >
                                {running ? (
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                ) : (
                                    <Play className="mr-2 h-4 w-4" />
                                )}
                                Run
                            </Button>
                        </div>

                        {testOpen && (
                            <div className="flex min-h-0 min-w-0 flex-1 gap-3 overflow-hidden border-t p-3">
                                <div className="flex w-56 shrink-0 flex-col xl:w-72">
                                    <Textarea
                                        value={payload}
                                        onChange={(e) => setPayload(e.target.value)}
                                        className="min-h-0 flex-1 resize-none font-mono text-xs"
                                        spellCheck={false}
                                    />
                                    <p className="mt-1 text-xs text-muted-foreground">
                                        Runs the saved draft, never the deployed version.
                                    </p>
                                </div>

                                <div className="min-h-0 min-w-0 flex-1 overflow-y-auto text-xs">
                                    {!outcome ? (
                                        <p className="text-muted-foreground">No run yet.</p>
                                    ) : (
                                        <div className="space-y-3">
                                            <div className="flex items-center gap-2">
                                                <span
                                                    className={`rounded px-1.5 py-0.5 font-medium ${
                                                        outcome.statusCode === 200
                                                            ? "bg-green-100 text-green-800"
                                                            : "bg-red-100 text-red-800"
                                                    }`}
                                                >
                                                    {outcome.statusCode}
                                                </span>
                                                {outcome.durationMs !== undefined && (
                                                    <span className="text-muted-foreground">
                                                        {outcome.durationMs} ms
                                                    </span>
                                                )}
                                            </div>

                                            {outcome.error && (
                                                <div>
                                                    <div className="mb-1 font-medium text-red-700">
                                                        Error
                                                    </div>
                                                    <pre className="whitespace-pre-wrap break-all rounded bg-red-50 p-2 font-mono text-red-800">
                                                        {outcome.error}
                                                    </pre>
                                                </div>
                                            )}

                                            {outcome.result !== undefined &&
                                                outcome.result !== null && (
                                                    <div>
                                                        <div className="mb-1 font-medium">
                                                            Result
                                                        </div>
                                                        <pre className="overflow-x-auto rounded bg-muted p-2 font-mono">
                                                            {JSON.stringify(outcome.result, null, 2)}
                                                        </pre>
                                                    </div>
                                                )}

                                            {outcome.logs && (
                                                <div>
                                                    <div className="mb-1 font-medium">Logs</div>
                                                    <pre className="whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono">
                                                        {outcome.logs}
                                                    </pre>
                                                </div>
                                            )}

                                            {outcome.traceback && (
                                                <details>
                                                    <summary className="cursor-pointer font-medium">
                                                        Traceback
                                                    </summary>
                                                    <pre className="mt-1 whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono">
                                                        {outcome.traceback}
                                                    </pre>
                                                </details>
                                            )}
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}
                        </div>
                    </>
                </section>

                {scoutEnabled && scoutOpen && (
                    <ResizeHandle
                        orientation="vertical"
                        label="Resize Scout"
                        onDelta={(delta) =>
                            setScoutWidth((width) => clampWidth(width - delta, 280, 640))
                        }
                        onDoubleClick={() => setScoutWidth(360)}
                    />
                )}

                {scoutEnabled && scoutOpen && (
                    <aside
                        className="flex min-w-0 shrink-0 flex-col overflow-hidden border-l"
                        style={{ width: scoutWidth }}
                    >
                        <WorkflowGenChatPanel
                            surface="code_editor"
                            onBusyChange={setScoutBusy}
                            onClose={() => setScoutOpen(false)}
                            emptyStateTitle="Ask me to write or change a function. I'll show you a diff before anything is saved, and I can run it to check my own work."
                            suggestions={[
                                "Write a function that looks up an order by number",
                                "Add error handling to the router",
                                "Test get_order_status with a sample order",
                            ]}
                        />
                    </aside>
                )}
            </div>

            {/* Versions & deploy */}
            <Dialog open={versionsOpen} onOpenChange={setVersionsOpen}>
                <DialogContent className="max-w-2xl">
                    <DialogHeader>
                        <DialogTitle>Versions &amp; Deploy</DialogTitle>
                        <DialogDescription>
                            A version freezes the whole workspace. Deploying turns its function
                            definitions into tools your agents can call — and archives tools whose
                            files you removed.
                        </DialogDescription>
                    </DialogHeader>

                    <div className="flex gap-2">
                        <Input
                            placeholder="What changed?"
                            value={versionNote}
                            onChange={(e) => setVersionNote(e.target.value)}
                        />
                        <Button onClick={handleCreateVersion} disabled={busyVersion}>
                            Create Version
                        </Button>
                    </div>

                    <div className="max-h-80 overflow-y-auto">
                        {versions.length === 0 ? (
                            <p className="py-6 text-center text-sm text-muted-foreground">
                                No versions yet.
                            </p>
                        ) : (
                            versions.map((version) => (
                                <div
                                    key={version.version_number}
                                    className="flex items-center justify-between border-b py-2 text-sm last:border-b-0"
                                >
                                    <div className="min-w-0">
                                        <div className="font-medium">
                                            v{version.version_number}
                                            {version.deployed_at && (
                                                <span className="ml-2 rounded bg-green-100 px-1.5 py-0.5 text-xs text-green-800">
                                                    deployed
                                                </span>
                                            )}
                                        </div>
                                        <div className="truncate text-xs text-muted-foreground">
                                            {version.description || "No description"} ·{" "}
                                            {version.file_count} file(s)
                                        </div>
                                    </div>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        disabled={busyVersion}
                                        onClick={() => handleDeploy(version.version_number)}
                                    >
                                        Deploy
                                    </Button>
                                </div>
                            ))
                        )}
                    </div>
                </DialogContent>
            </Dialog>

            {/* Environment variables */}
            <Dialog open={envOpen} onOpenChange={setEnvOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>Environment Variables</DialogTitle>
                        <DialogDescription>
                            Encrypted and injected as <code>os.environ</code> when your code runs.
                            Never put a key in the Python or the JSON. A stored value is not shown
                            again.
                        </DialogDescription>
                    </DialogHeader>

                    <div className="space-y-2">
                        {envVars.map((variable) => (
                            <div
                                key={variable.key}
                                className="flex items-center justify-between rounded border px-3 py-2 text-sm"
                            >
                                <div>
                                    <div className="font-mono">{variable.key}</div>
                                    <div className="text-xs text-muted-foreground">
                                        {variable.hint ? `…${variable.hint}` : "set"}
                                    </div>
                                </div>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={async () => {
                                        await deleteEnvVar(variable.key);
                                        setEnvVars(await listEnvVars());
                                    }}
                                >
                                    <Trash2 className="h-4 w-4" />
                                </Button>
                            </div>
                        ))}
                        {envVars.length === 0 && (
                            <p className="py-2 text-sm text-muted-foreground">None yet.</p>
                        )}
                    </div>

                    <div className="space-y-2 border-t pt-3">
                        <Label>Add a variable</Label>
                        <div className="flex gap-2">
                            <Input
                                placeholder="ORDERS_API_KEY"
                                value={newKey}
                                onChange={(e) => setNewKey(e.target.value)}
                                className="font-mono"
                            />
                            <Input
                                type="password"
                                placeholder="value"
                                value={newValue}
                                onChange={(e) => setNewValue(e.target.value)}
                            />
                            <Button onClick={handleAddEnv}>Add</Button>
                        </div>
                    </div>
                </DialogContent>
            </Dialog>

            {/* New file */}
            <Dialog open={newFileOpen} onOpenChange={setNewFileOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>New file</DialogTitle>
                        <DialogDescription>
                            A function schema goes in <code>function_definitions/</code> and its
                            filename must match the schema&apos;s <code>name</code>. Helper modules
                            can be any <code>.py</code> path.
                        </DialogDescription>
                    </DialogHeader>
                    <Input
                        value={newFilePath}
                        onChange={(e) => setNewFilePath(e.target.value)}
                        placeholder="function_definitions/check_stock.json"
                        className="font-mono"
                    />
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setNewFileOpen(false)}>
                            Cancel
                        </Button>
                        <Button onClick={handleNewFile}>Create</Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </main>
    );
}
