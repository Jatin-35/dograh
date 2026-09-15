import { client } from "@/client/client.gen";

/**
 * Code Editor API.
 *
 * Uses the shared generated `client` directly rather than typed SDK helpers,
 * because these routes postdate the last `npm run generate-client`. It still
 * routes through the same base URL and auth interceptor as every other request.
 */

export interface CodeFile {
    path: string;
    content: string;
}

export interface SaveResult {
    path: string;
    warnings: string[];
}

export interface EnvVar {
    key: string;
    hint: string | null;
    /** The value's real length — null for a row saved before this was
     * tracked, which has no way to recover it. Lets the UI mask with exactly
     * as many placeholder characters as the value actually has. */
    length: number | null;
}

/** `**********a1b2` — as many mask characters as the value is actually long,
 * ending in its real hint. Falls back to a fixed-width mask when `length` is
 * unknown (a row saved before it was tracked) rather than guessing a length
 * that might be wrong, and to a plain label when there's no hint at all
 * (a value under 8 characters reveals nothing, by design). */
export function formatMaskedValue(variable: EnvVar): string {
    if (!variable.hint) return variable.length == null ? "set" : `set (${variable.length} chars)`;
    if (variable.length == null) return `****${variable.hint}`;
    const maskCount = Math.max(0, variable.length - variable.hint.length);
    return `${"*".repeat(maskCount)}${variable.hint}`;
}

export interface CodeVersion {
    version_number: number;
    description: string | null;
    created_at: string;
    deployed_at: string | null;
    file_count: number;
}

export interface RunOutcome {
    statusCode: number;
    result?: unknown;
    logs?: string;
    error?: string;
    traceback?: string;
    durationMs?: number;
}

export interface DeployReport {
    version_number: number;
    created: string[];
    updated: string[];
    archived: string[];
    unchanged: string[];
    errors: string[];
}

/** A save rejected for validation carries the specific problems, which the
 * editor shows against the file rather than as a generic toast. */
export class CodeEditorError extends Error {
    errors: string[];

    constructor(message: string, errors: string[] = []) {
        super(message);
        this.name = "CodeEditorError";
        this.errors = errors;
    }
}

function fail(error: unknown, fallback: string): never {
    const detail = (error as { detail?: unknown })?.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
        const shaped = detail as { message?: string; errors?: string[] };
        throw new CodeEditorError(shaped.message || fallback, shaped.errors || []);
    }
    if (typeof detail === "string") {
        throw new CodeEditorError(detail);
    }
    throw new CodeEditorError(fallback);
}

const BASE = "/api/v1/code-editor";

export async function listFiles(): Promise<CodeFile[]> {
    const { data, error } = await client.get<CodeFile[]>({ url: `${BASE}/files` });
    if (error || !data) fail(error, "Failed to load the workspace");
    return data;
}

export async function saveFile(path: string, content: string): Promise<SaveResult> {
    const { data, error } = await client.put<SaveResult>({
        url: `${BASE}/files/${path}`,
        body: { content },
    });
    if (error || !data) fail(error, `Failed to save ${path}`);
    return data;
}

export async function deleteFile(path: string): Promise<void> {
    const { error } = await client.delete({ url: `${BASE}/files/${path}` });
    if (error) fail(error, `Failed to delete ${path}`);
}

export async function testRun(
    event: Record<string, unknown>,
    timeoutSeconds = 10,
): Promise<RunOutcome> {
    const { data, error } = await client.post<RunOutcome>({
        url: `${BASE}/test-run`,
        body: { event, timeout_seconds: timeoutSeconds },
    });
    if (error || !data) fail(error, "The test run could not be started");
    return data;
}

/** Runs whatever is currently deployed, untouched by unsaved draft edits. */
export async function testRunDeployed(
    event: Record<string, unknown>,
    timeoutSeconds = 10,
): Promise<RunOutcome> {
    const { data, error } = await client.post<RunOutcome>({
        url: `${BASE}/test-run-deployed`,
        body: { event, timeout_seconds: timeoutSeconds },
    });
    if (error || !data) fail(error, "The deployed run could not be started");
    return data;
}

export async function listVersions(): Promise<CodeVersion[]> {
    const { data, error } = await client.get<CodeVersion[]>({ url: `${BASE}/versions` });
    if (error || !data) fail(error, "Failed to load versions");
    return data;
}

export async function createVersion(description: string): Promise<CodeVersion> {
    const { data, error } = await client.post<CodeVersion>({
        url: `${BASE}/versions`,
        body: { description },
    });
    if (error || !data) fail(error, "Failed to create a version");
    return data;
}

export async function deployVersion(versionNumber: number): Promise<DeployReport> {
    const { data, error } = await client.post<DeployReport>({
        url: `${BASE}/versions/${versionNumber}/deploy`,
    });
    if (error || !data) fail(error, `Failed to deploy v${versionNumber}`);
    return data;
}

export async function listEnvVars(): Promise<EnvVar[]> {
    const { data, error } = await client.get<EnvVar[]>({ url: `${BASE}/env` });
    if (error || !data) fail(error, "Failed to load environment variables");
    return data;
}

export async function setEnvVar(key: string, value: string): Promise<EnvVar> {
    const { data, error } = await client.put<EnvVar>({
        url: `${BASE}/env/${key}`,
        body: { value },
    });
    if (error || !data) fail(error, `Failed to save ${key}`);
    return data;
}

export async function deleteEnvVar(key: string): Promise<void> {
    const { error } = await client.delete({ url: `${BASE}/env/${key}` });
    if (error) fail(error, `Failed to delete ${key}`);
}

const FUNCTION_DIR = "function_definitions/";

/** Whether this path is a function schema — the only files the Test
 * Latest/Test Deployed/Docs panel applies to. Every other file (the router,
 * anything under agents/) keeps no test panel: there's no single function to
 * pre-fill a payload for. */
export function isFunctionDefinitionPath(path: string): boolean {
    return path.startsWith(FUNCTION_DIR) && path.endsWith(".json");
}

/** A JSON-schema-typed placeholder, matching the shape the Docs tab shows
 * (`"param1": "value1", "param2": 123`) rather than an empty/null value that
 * would make the sample payload look broken before anyone has touched it. */
function samplePropertyValue(schemaType: unknown): unknown {
    switch (schemaType) {
        case "integer":
        case "number":
            return 0;
        case "boolean":
            return false;
        case "array":
            return [];
        case "object":
            return {};
        default:
            return "value";
    }
}

/** Parsed shape of a function_definitions/*.json file — just enough to build
 * a sample payload and the Docs tab's worked example. */
export interface ParsedFunctionSchema {
    name: string;
    parameters: Array<{ name: string; type: unknown; example: unknown }>;
}

/** Best-effort parse. A file mid-edit is often momentarily invalid JSON —
 * this returns null rather than throwing, so the panel can fall back to a
 * filename-derived function_name instead of blocking on it. */
export function parseFunctionSchema(
    path: string,
    content: string,
): ParsedFunctionSchema | null {
    let schema: unknown;
    try {
        schema = JSON.parse(content);
    } catch {
        return null;
    }
    if (typeof schema !== "object" || schema === null) return null;
    const record = schema as Record<string, unknown>;
    const name =
        typeof record.name === "string" && record.name
            ? record.name
            : path.slice(FUNCTION_DIR.length).replace(/\.json$/, "");

    const params = record.parameters;
    const properties =
        typeof params === "object" && params !== null
            ? (params as Record<string, unknown>).properties
            : undefined;

    const parameters: ParsedFunctionSchema["parameters"] = [];
    if (typeof properties === "object" && properties !== null) {
        for (const [propName, prop] of Object.entries(
            properties as Record<string, unknown>,
        )) {
            const propType =
                typeof prop === "object" && prop !== null
                    ? (prop as Record<string, unknown>).type
                    : undefined;
            parameters.push({
                name: propName,
                type: propType,
                example: samplePropertyValue(propType),
            });
        }
    }
    return { name, parameters };
}

/** The EVENT JSON a Test Latest/Test Deployed run starts from: the function's
 * own name plus one example value per declared parameter. Regenerated only
 * the first time a file is opened — edits after that are the user's, exactly
 * like the code editor's own drafts, and must never be silently overwritten
 * by a later schema change. */
export function buildSampleEventPayload(schema: ParsedFunctionSchema): string {
    const event: Record<string, unknown> = { function_name: schema.name };
    for (const param of schema.parameters) {
        event[param.name] = param.example;
    }
    return JSON.stringify(event, null, 2);
}

/** The workspace path behind a Monaco model URI.
 *
 * The editor is handed bare relative paths (`agents/helper.py`), which
 * `Uri.parse` keeps verbatim, but a leading slash is a legal normalisation and
 * an absent URI means the editor has no model at all. Returning null rather
 * than guessing lets the caller decline to write anything. */
export function pathFromModelUri(uri: { path?: string } | null | undefined): string | null {
    const path = uri?.path?.replace(/^\/+/, "");
    return path ? path : null;
}

/** Next `drafts` state after the editor reports `value` for `path`.
 *
 * Returns `prev` unchanged — same object identity, so React bails out of the
 * re-render — whenever there is nothing to record. That matters for more than
 * performance: the editor emits content events during a file switch that carry
 * the incoming file's own text, and recording those would mark a file dirty
 * that nobody has edited. Comparing against what is stored makes such an echo
 * a no-op, and makes typing a change back to its saved text clear the dirty
 * mark instead of leaving a phantom unsaved file behind.
 *
 * `storedContent` is undefined for a path that isn't a real file — a virtual
 * view like `.env`, or a file deleted while the editor still held it. Those
 * are refused outright: nothing that isn't a saveable file may enter drafts. */
export function nextDrafts(
    prev: Record<string, string>,
    path: string,
    value: string,
    storedContent: string | undefined,
): Record<string, string> {
    if (storedContent === undefined) return prev;
    if (storedContent === value) {
        if (!(path in prev)) return prev;
        const next = { ...prev };
        delete next[path];
        return next;
    }
    if (prev[path] === value) return prev;
    return { ...prev, [path]: value };
}

/** Monaco's language id for a workspace path. */
export function languageFor(path: string): string {
    if (path.endsWith(".json")) return "json";
    if (path.endsWith(".ts")) return "typescript";
    if (path.endsWith(".py")) return "python";
    return "plaintext";
}

/** Group flat paths into the folders the tree renders.
 *
 * The workspace is intentionally shallow — a router, function definitions and
 * agents — so one level of grouping is all the structure there is to show. */
export function groupByFolder(files: CodeFile[]): Map<string, CodeFile[]> {
    const groups = new Map<string, CodeFile[]>();
    for (const file of files) {
        const slash = file.path.indexOf("/");
        const folder = slash === -1 ? "" : file.path.slice(0, slash);
        const existing = groups.get(folder);
        if (existing) existing.push(file);
        else groups.set(folder, [file]);
    }
    return groups;
}
