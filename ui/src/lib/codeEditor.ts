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
