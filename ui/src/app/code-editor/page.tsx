"use client";

import Editor, { type EditorProps } from "@monaco-editor/react";
import {
    AlertTriangle,
    ChevronDown,
    ChevronRight,
    FileCode,
    FileJson,
    FileText,
    KeyRound,
    Loader2,
    PanelLeftClose,
    PanelLeftOpen,
    Pencil,
    Plus,
    Rocket,
    Save,
    Settings,
    Sparkles,
    Trash2,
    X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { FunctionTestPanel } from "@/components/code-editor/FunctionTestPanel";
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
    formatMaskedValue,
    groupByFolder,
    isFunctionDefinitionPath,
    languageFor,
    listEnvVars,
    listFiles,
    listVersions,
    nextDrafts,
    pathFromModelUri,
    saveFile,
    setEnvVar,
} from "@/lib/codeEditor";

const ENTRY_POINT = "all_events_entry_point.py";
const LAYOUT_KEY = "dograh:code-editor:layout";
// Not a real workspace file — never saved, never sent to the sandbox, never
// visible to Scout's file tools. Purely a read-only view of the same
// variables the Variables dialog manages, so the workspace looks and feels
// like a normal project that has a .env instead of a list behind a button.
// Real values are never in it, only the same last-few-characters hint shown
// everywhere else — see api/services/code_editor/secrets.py::hint.
const ENV_FILE_PATH = ".env";

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

    const [versions, setVersions] = useState<CodeVersion[]>([]);
    const [versionsOpen, setVersionsOpen] = useState(false);
    const [versionNote, setVersionNote] = useState("");
    const [busyVersion, setBusyVersion] = useState(false);

    const [envOpen, setEnvOpen] = useState(false);
    const [envVars, setEnvVars] = useState<EnvVar[]>([]);
    const [newKey, setNewKey] = useState("");
    const [newValue, setNewValue] = useState("");
    // Set while replacing an existing variable's value rather than adding a
    // new one — the key becomes read-only so retyping it can't accidentally
    // create a second, slightly-misspelled variable alongside the one meant.
    const [editingKey, setEditingKey] = useState<string | null>(null);

    const [newFileOpen, setNewFileOpen] = useState(false);
    const [newFilePath, setNewFilePath] = useState("function_definitions/");
    const [creatingFile, setCreatingFile] = useState(false);

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

    // Regenerated from the real, encrypted variables every time the list
    // changes — never stored, never editable, never the actual value.
    const envFileContent = useMemo(() => {
        const header =
            "# Read-only. Real values are encrypted in the database, never on disk —\n" +
            "# this exists so variables show up the way a normal project's .env would.\n" +
            "# Manage real values from the Variables button above.\n\n";
        if (envVars.length === 0) return `${header}# No variables set yet.\n`;
        return (
            header +
            envVars
                .map((v) => `${v.key}=${formatMaskedValue(v)}`)
                .join("\n") +
            "\n"
        );
    }, [envVars]);

    // Deliberately has no .env branch: the virtual file is rendered from
    // `envFileContent` directly and must never enter the real-file pipeline
    // (editor model, drafts, save), which is how its text ended up written
    // into a real file's draft once already.
    const activeContent = useMemo(() => {
        if (activePath in drafts) return drafts[activePath];
        return files.find((f) => f.path === activePath)?.content ?? "";
    }, [activePath, drafts, files]);

    const isDirty = activePath in drafts;
    const dirtyCount = Object.keys(drafts).length;

    // Read by recordEdit, which runs from a Monaco subscription rather than a
    // React render, so it must not close over a render's `files`.
    const filesRef = useRef<CodeFile[]>([]);
    filesRef.current = files;

    const editorRef = useRef<Parameters<NonNullable<EditorProps["onMount"]>>[0] | null>(
        null,
    );

    // Lets work that outlives a render — a save that is still in flight when
    // the user clicks another file — ask what is on screen *now* rather than
    // trusting the path its closure captured.
    const activePathRef = useRef(activePath);
    activePathRef.current = activePath;

    /** Record an edit against the file Monaco itself says the text came from.
     *
     * Not against `activePath`. @monaco-editor/react re-subscribes onChange in
     * an effect declared *after* the effects that swap the model and push the
     * new file's text into it, so for one commit the live subscription still
     * holds the previous render's closure. Any content event that escapes the
     * library's suppression window during that commit — a deferred
     * trim-trailing-whitespace edit, an end-of-line normalisation — is then
     * attributed to the file the user just navigated away from. That is how
     * the masked `.env` listing ended up saved as the body of
     * all_events_entry_point.py. The model's own URI is the one source that
     * cannot lag, because it is what produced the text. */
    const recordEdit = useCallback((value: string) => {
        const path = pathFromModelUri(editorRef.current?.getModel()?.uri);
        if (!path) return;
        // Undefined for a path with no saveable file behind it: the virtual
        // .env, a file deleted while the editor still held its model, or the
        // empty buffer shown before the workspace has finished loading.
        // Dropping the text is the safe outcome in all three — the alternative
        // is staging an unsaved change against a file nobody edited.
        const stored = filesRef.current.find((file) => file.path === path)?.content;
        setDrafts((prev) => nextDrafts(prev, path, value, stored));
    }, []);

    // A failed save's "Problems" belong to the file that failed, not to
    // whatever the user looks at next — without this, switching to an
    // unrelated file kept showing the previous file's error underneath it
    // (e.g. a Python syntax error from the router, still visible after
    // navigating to a JSON schema that was never the file with the problem).
    useEffect(() => {
        setProblems([]);
    }, [activePath]);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const loaded = await listFiles();
            setFiles(loaded);
            if (!loaded.some((f) => f.path === activePath) && activePath !== ENV_FILE_PATH) {
                setActivePath(loaded[0]?.path ?? ENTRY_POINT);
            }
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load");
        } finally {
            setLoading(false);
        }
        // The virtual .env view's content — best-effort: a failure here just
        // means it shows stale hints until the next sync, not a page error.
        try {
            setEnvVars(await listEnvVars());
        } catch {
            // Nothing to do — the dialog's own "Variables" button retries this.
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
        try {
            setEnvVars(await listEnvVars());
        } catch {
            // The virtual .env view just keeps showing what it already had.
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
            current === ENV_FILE_PATH || loaded.some((file) => file.path === current)
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
        // Belt and braces: .env is not a real file and has nothing to persist.
        // It can never be dirty today, but Ctrl+S is one keystroke and this
        // costs nothing.
        if (activePath === ENV_FILE_PATH || !isDirty) return;
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
                // Only if this file is still the one on screen. A save is
                // async and the sidebar stays clickable throughout; the
                // Problems panel is unlabelled, so warnings shown after the
                // user has moved on read as problems with the file they are
                // now looking at.
                if (activePathRef.current === activePath) setProblems(result.warnings);
                toast.warning(
                    `Saved ${activePath} with ${result.warnings.length} warning(s).`,
                );
            } else {
                toast.success(`Saved ${activePath}`);
            }
        } catch (error) {
            // Validation problems belong against the file, not in a toast that
            // vanishes before it can be acted on.
            if (error instanceof CodeEditorError && error.errors.length) {
                if (activePathRef.current === activePath) setProblems(error.errors);
                toast.error(`${activePath} not saved — see the problems below.`);
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
        setEditingKey(null);
        setNewKey("");
        setNewValue("");
        try {
            setEnvVars(await listEnvVars());
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load");
        }
    };

    const handleAddEnv = async () => {
        if (!newKey.trim() || !newValue) return;
        try {
            const key = newKey.trim();
            const saved = await setEnvVar(key, newValue);
            const wasEditing = editingKey !== null;
            setNewKey("");
            setNewValue("");
            setEditingKey(null);
            setEnvVars(await listEnvVars());
            // The hint (last few characters) is the only confirmation that the
            // right value actually landed — a stored value is never shown
            // again, so this is the one moment to catch a fat-fingered paste.
            // Short values (<8 chars) get no hint at all; saying so plainly
            // beats a toast that looks like it forgot to fill in a blank.
            const confirmation = saved.hint
                ? `ends in …${saved.hint}`
                : "too short to show a hint";
            toast.success(
                wasEditing
                    ? `Updated ${key} (${confirmation}).`
                    : `Saved ${key} (${confirmation}).`,
            );
        } catch (error) {
            if (error instanceof CodeEditorError && error.errors.length) {
                toast.error(`${error.message} ${error.errors[0]}`);
            } else {
                toast.error(error instanceof Error ? error.message : "Failed to save");
            }
        }
    };

    const handleEditEnv = (key: string) => {
        setEditingKey(key);
        setNewKey(key);
        setNewValue("");
    };

    const handleCancelEditEnv = () => {
        setEditingKey(null);
        setNewKey("");
        setNewValue("");
    };

    const handleDeleteEnv = async (key: string) => {
        try {
            await deleteEnvVar(key);
            setEnvVars(await listEnvVars());
            if (editingKey === key) handleCancelEditEnv();
            toast.success(`Deleted ${key}`);
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to delete");
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

    const handleNewFile = async () => {
        const path = newFilePath.trim();
        if (!path || files.some((f) => f.path === path)) {
            toast.error(path ? "That file already exists." : "Enter a path.");
            return;
        }
        const starter = path.endsWith(".json")
            ? JSON.stringify(
                  {
                      name: path.split("/").pop()?.replace(".json", ""),
                      description: "TODO: describe what this function does.",
                      parameters: { type: "object", properties: {}, required: [] },
                  },
                  null,
                  2,
              )
            : "";

        // Persisted immediately, not just added to local state: a file that
        // exists only in the browser is invisible to the server, so the very
        // next background refresh — Scout finishing a turn, or switching back
        // to this tab — refetches the real file list and silently discards
        // it, with no error. That was the "glitches and doesn't get created"
        // report: the file never existed past this dialog closing.
        setCreatingFile(true);
        try {
            await saveFile(path, starter);
        } catch (error) {
            if (error instanceof CodeEditorError && error.errors.length) {
                toast.error(`${error.message} ${error.errors[0]}`);
            } else {
                toast.error(error instanceof Error ? error.message : "Failed to create the file");
            }
            return;
        } finally {
            setCreatingFile(false);
        }

        setFiles((prev) =>
            [...prev, { path, content: starter }].sort((a, b) => a.path.localeCompare(b.path)),
        );
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
                        <>
                        {/* Not a real file — see ENV_FILE_PATH. Shown above the
                            router the way a project's .env usually sits at the
                            root, ahead of everything else. */}
                        <button
                            type="button"
                            title="Read-only — manage real values from Variables"
                            className={`mb-1 flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm ${
                                activePath === ENV_FILE_PATH
                                    ? "bg-accent font-medium"
                                    : "hover:bg-accent/50"
                            }`}
                            onClick={() => setActivePath(ENV_FILE_PATH)}
                        >
                            <KeyRound className="h-4 w-4 shrink-0 text-muted-foreground" />
                            <span className="truncate">{ENV_FILE_PATH}</span>
                        </button>
                        {[...grouped.entries()].map(([folder, folderFiles]) => (
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
                        ))}
                        </>
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
                        {activePath === ENV_FILE_PATH ? (
                            <span>read-only — hints only, manage real values from Variables</span>
                        ) : (
                            isDirty && <span className="text-amber-600">unsaved</span>
                        )}
                    </div>

                    <div className="min-h-0 flex-1">
                        {/* The virtual .env is rendered outside Monaco entirely, not as a
                            read-only model. Giving Monaco a model for it meant that
                            switching away fired `onChange` mid-swap — carrying the .env
                            text but the newly-selected path — which wrote .env's content
                            into a real file's draft. Guarding onChange by path can't fix
                            that: at that moment the path has already changed. Keeping
                            Monaco to real files only removes the race rather than
                            narrowing it. */}
                        {activePath === ENV_FILE_PATH ? (
                            <div className="h-full overflow-auto p-4">
                                <pre className="font-mono text-xs leading-relaxed text-muted-foreground">
                                    {envFileContent}
                                </pre>
                            </div>
                        ) : (
                            <Editor
                                height="100%"
                                path={activePath}
                                language={languageFor(activePath)}
                                value={activeContent}
                                onMount={(editor) => {
                                    editorRef.current = editor;
                                }}
                                onChange={(value) => recordEdit(value ?? "")}
                                options={{
                                    minimap: { enabled: false },
                                    fontSize: 13,
                                    tabSize: 4,
                                    scrollBeyondLastLine: false,
                                    automaticLayout: true,
                                }}
                            />
                        )}
                    </div>

                    {problems.length > 0 && (
                        <div className="max-h-32 shrink-0 overflow-y-auto border-t bg-red-50 px-3 py-2 text-xs text-red-700">
                            <div className="mb-1 flex items-center gap-1 font-medium">
                                <AlertTriangle className="h-3.5 w-3.5" />
                                {/* Named, not just "Problems". An unlabelled
                                    panel is what made a failed save's errors
                                    look like they belonged to whatever file
                                    was open at the time. */}
                                Problems in{" "}
                                <span className="font-mono font-normal">{activePath}</span>
                                <button
                                    type="button"
                                    className="ml-auto rounded p-0.5 text-red-700/70 hover:bg-red-100 hover:text-red-900"
                                    title="Dismiss"
                                    aria-label="Dismiss problems"
                                    onClick={() => setProblems([])}
                                >
                                    <X className="h-3.5 w-3.5" />
                                </button>
                            </div>
                            {problems.map((problem) => (
                                <div key={problem} className="font-mono">
                                    {problem}
                                </div>
                            ))}
                        </div>
                    )}
                    {/* Test Latest / Test Deployed / Docs sits under the editor rather
                        than beside it: four side-by-side columns overflowed the
                        viewport, and this is the panel that needs width more than
                        height. Only for a function schema — every other file (the
                        router, agents/ helpers) has no single function_name to test
                        against, so no panel replaces the space at all. */}
                    {isFunctionDefinitionPath(activePath) && (
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
                                style={testOpen ? { height: testHeight } : { height: 32 }}
                            >
                                <button
                                    type="button"
                                    className="flex items-center gap-1 px-3 py-1.5 text-left text-xs font-medium uppercase text-muted-foreground hover:text-foreground"
                                    onClick={() => setTestOpen((open) => !open)}
                                >
                                    {testOpen ? (
                                        <ChevronDown className="h-3.5 w-3.5" />
                                    ) : (
                                        <ChevronRight className="h-3.5 w-3.5" />
                                    )}
                                    Test
                                </button>
                                {testOpen && (
                                    <FunctionTestPanel
                                        activePath={activePath}
                                        schemaContent={activeContent}
                                        dirtyCount={dirtyCount}
                                    />
                                )}
                            </div>
                        </>
                    )}
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
                                className={`flex items-center justify-between rounded border px-3 py-2 text-sm ${
                                    editingKey === variable.key ? "border-primary bg-accent/40" : ""
                                }`}
                            >
                                <div>
                                    <div className="font-mono">{variable.key}</div>
                                    <div className="text-xs text-muted-foreground font-mono">
                                        {formatMaskedValue(variable)}
                                    </div>
                                </div>
                                <div className="flex items-center">
                                    <Button
                                        variant="ghost"
                                        size="sm"
                                        title={`Replace ${variable.key}'s value`}
                                        onClick={() => handleEditEnv(variable.key)}
                                    >
                                        <Pencil className="h-4 w-4" />
                                    </Button>
                                    <Button
                                        variant="ghost"
                                        size="sm"
                                        title={`Delete ${variable.key}`}
                                        onClick={() => handleDeleteEnv(variable.key)}
                                    >
                                        <Trash2 className="h-4 w-4" />
                                    </Button>
                                </div>
                            </div>
                        ))}
                        {envVars.length === 0 && (
                            <p className="py-2 text-sm text-muted-foreground">None yet.</p>
                        )}
                    </div>

                    <div className="space-y-2 border-t pt-3">
                        <div className="flex items-center justify-between">
                            <Label>
                                {editingKey ? `Replace the value of ${editingKey}` : "Add a variable"}
                            </Label>
                            {editingKey && (
                                <button
                                    type="button"
                                    className="text-xs text-muted-foreground hover:text-foreground"
                                    onClick={handleCancelEditEnv}
                                >
                                    Cancel
                                </button>
                            )}
                        </div>
                        {/* A stored value is never shown again — "editing" can only ever
                            mean supplying a brand new value, never revealing the old one.
                            The key is locked while editing so retyping it can't create a
                            second, slightly-misspelled variable next to the one intended. */}
                        <div className="flex gap-2">
                            <Input
                                placeholder="ORDERS_API_KEY"
                                value={newKey}
                                onChange={(e) => setNewKey(e.target.value)}
                                className="font-mono"
                                disabled={editingKey !== null}
                            />
                            <Input
                                type="password"
                                placeholder={editingKey ? "new value" : "value"}
                                value={newValue}
                                onChange={(e) => setNewValue(e.target.value)}
                                autoFocus={editingKey !== null}
                            />
                            <Button onClick={handleAddEnv}>{editingKey ? "Update" : "Add"}</Button>
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
                        <Button
                            variant="outline"
                            onClick={() => setNewFileOpen(false)}
                            disabled={creatingFile}
                        >
                            Cancel
                        </Button>
                        <Button onClick={handleNewFile} disabled={creatingFile}>
                            {creatingFile && (
                                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            )}
                            Create
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </main>
    );
}
