/**
 * The pure logic behind the Test Latest / Test Deployed panel's auto-filled
 * EVENT JSON.
 *
 * This is the fix for the bug reported against the old flat Test Run strip:
 * its payload box was a single hardcoded default that never updated when you
 * switched files, so testing `check_pending_complaint_ticket.json` could
 * silently send a stale `function_name` from whatever file you'd last been
 * on — the router correctly said "unknown function" for a function that
 * plainly existed in the file list. These tests pin the replacement: the
 * payload is derived from the *active file's own schema*, every time.
 */

import { describe, expect, it } from "vitest";

import {
    buildSampleEventPayload,
    type EnvVar,
    formatMaskedValue,
    isFunctionDefinitionPath,
    nextDrafts,
    parseFunctionSchema,
    pathFromModelUri,
} from "./codeEditor";

describe("isFunctionDefinitionPath", () => {
    it.each([
        "function_definitions/check_order_status.json",
        "function_definitions/nested/thing.json",
    ])("is true for %s", (path) => {
        expect(isFunctionDefinitionPath(path)).toBe(true);
    });

    it.each([
        "all_events_entry_point.py",
        "agents/helper.ts",
        "function_definitions/check_order_status.py",
        "not_function_definitions/check_order_status.json",
    ])("is false for %s — the panel must not apply here", (path) => {
        expect(isFunctionDefinitionPath(path)).toBe(false);
    });
});

describe("parseFunctionSchema", () => {
    const SCHEMA = JSON.stringify({
        name: "check_order_status",
        description: "Looks up an order.",
        parameters: {
            type: "object",
            properties: {
                order_id: { type: "string", description: "The order number." },
                retry_count: { type: "integer" },
                urgent: { type: "boolean" },
            },
            required: ["order_id"],
        },
    });

    it("reads the declared name and every parameter's type", () => {
        const schema = parseFunctionSchema(
            "function_definitions/check_order_status.json",
            SCHEMA,
        );
        expect(schema?.name).toBe("check_order_status");
        expect(schema?.parameters.map((p) => p.name).sort()).toEqual(
            ["order_id", "retry_count", "urgent"].sort(),
        );
    });

    it("falls back to the filename when JSON is invalid — a file mid-edit", () => {
        const schema = parseFunctionSchema(
            "function_definitions/check_pending_complaint_ticket.json",
            "{ this is not valid json",
        );
        // The whole point: this must not throw, and must not silently return
        // a stale/unrelated function_name — it names *this* file.
        expect(schema).toBeNull();
    });

    it("falls back to the filename when the schema has no name field", () => {
        const schema = parseFunctionSchema(
            "function_definitions/check_pending_complaint_ticket.json",
            JSON.stringify({ parameters: { properties: {} } }),
        );
        expect(schema?.name).toBe("check_pending_complaint_ticket");
    });

    it("returns no parameters for a schema with none declared", () => {
        const schema = parseFunctionSchema(
            "function_definitions/ping.json",
            JSON.stringify({ name: "ping" }),
        );
        expect(schema?.parameters).toEqual([]);
    });
});

describe("buildSampleEventPayload", () => {
    it("always includes function_name, matching the file it came from", () => {
        const payload = buildSampleEventPayload({ name: "check_order_status", parameters: [] });
        expect(JSON.parse(payload)).toEqual({ function_name: "check_order_status" });
    });

    it("gives each declared parameter a type-appropriate example value", () => {
        const payload = buildSampleEventPayload({
            name: "create_ticket",
            parameters: [
                { name: "summary", type: "string", example: "value" },
                { name: "priority", type: "integer", example: 0 },
                { name: "urgent", type: "boolean", example: false },
                { name: "tags", type: "array", example: [] },
                { name: "meta", type: "object", example: {} },
            ],
        });
        expect(JSON.parse(payload)).toEqual({
            function_name: "create_ticket",
            summary: "value",
            priority: 0,
            urgent: false,
            tags: [],
            meta: {},
        });
    });

    it("produces the exact function_name a different file's stale payload would get wrong", () => {
        // The regression case, made concrete: two different function files
        // must each get their own, correct function_name — never each
        // other's, and never a hardcoded default from a third file.
        const a = parseFunctionSchema(
            "function_definitions/check_order_status.json",
            JSON.stringify({ name: "check_order_status" }),
        )!;
        const b = parseFunctionSchema(
            "function_definitions/check_pending_complaint_ticket.json",
            JSON.stringify({ name: "check_pending_complaint_ticket" }),
        )!;

        expect(JSON.parse(buildSampleEventPayload(a)).function_name).toBe(
            "check_order_status",
        );
        expect(JSON.parse(buildSampleEventPayload(b)).function_name).toBe(
            "check_pending_complaint_ticket",
        );
    });
});

describe("formatMaskedValue", () => {
    const v = (overrides: Partial<EnvVar>): EnvVar => ({
        key: "SOME_KEY",
        hint: null,
        length: null,
        ...overrides,
    });

    it("masks with exactly as many characters as the value actually has", () => {
        // A 19-character value gets a 6-character hint (see secrets.hint's
        // tiering) — so 13 mask characters, not a fixed, made-up count.
        const masked = formatMaskedValue(v({ hint: "klmnop", length: 19 }));
        expect(masked).toBe("*".repeat(13) + "klmnop");
        expect(masked.length).toBe(19);
    });

    it("a short value gets no visible characters, but still the real length", () => {
        // "short" (5 chars) is below the hint threshold entirely.
        expect(formatMaskedValue(v({ hint: null, length: 5 }))).toBe("set (5 chars)");
    });

    it("falls back to a fixed mask when length predates this column", () => {
        // A row saved before value_length existed: there is no way to know
        // how long the original value was, so this must not fabricate one.
        expect(formatMaskedValue(v({ hint: "3456", length: null }))).toBe("****3456");
    });

    it("falls back to a plain label when neither hint nor length exist", () => {
        expect(formatMaskedValue(v({ hint: null, length: null }))).toBe("set");
    });

    it("never produces a negative mask count even if hint were longer than length", () => {
        // Defensive: these two numbers only ever disagree if something
        // upstream is already broken, but this must not render "-2" asterisks.
        expect(formatMaskedValue(v({ hint: "abcdef", length: 4 }))).toBe("abcdef");
    });
});

/**
 * Regression cover for the bug that shipped: the masked `.env` listing was
 * saved as the body of `all_events_entry_point.py`.
 *
 * Root cause was attribution, not rendering. @monaco-editor/react re-subscribes
 * its onChange handler in an effect declared *after* the effects that swap the
 * model and push the new file's text into it, so for one commit the live
 * subscription still holds the previous render's closure — and any content
 * event escaping the library's suppression window in that commit is filed
 * against the file the user just left. These tests pin the replacement rule:
 * the text is filed against the model that produced it, and never against a
 * path that isn't a saveable file.
 */
describe("pathFromModelUri", () => {
    it("reads the bare workspace path Monaco was given", () => {
        expect(pathFromModelUri({ path: "all_events_entry_point.py" })).toBe(
            "all_events_entry_point.py",
        );
        expect(pathFromModelUri({ path: "function_definitions/check.json" })).toBe(
            "function_definitions/check.json",
        );
    });

    it("tolerates the leading slash Uri normalisation may add", () => {
        expect(pathFromModelUri({ path: "/agents/helper.py" })).toBe("agents/helper.py");
    });

    it("returns null when there is no model — nothing may be attributed", () => {
        expect(pathFromModelUri(null)).toBeNull();
        expect(pathFromModelUri(undefined)).toBeNull();
        expect(pathFromModelUri({ path: "" })).toBeNull();
        expect(pathFromModelUri({ path: "/" })).toBeNull();
    });
});

describe("nextDrafts", () => {
    const FILES = {
        "all_events_entry_point.py": "def route(event, context):\n    pass\n",
    };
    const stored = (path: string) => (FILES as Record<string, string>)[path];

    it("records a genuine edit against its own file", () => {
        const next = nextDrafts({}, "all_events_entry_point.py", "edited", stored("all_events_entry_point.py"));
        expect(next).toEqual({ "all_events_entry_point.py": "edited" });
    });

    it("refuses a path that is not a real file — the .env leak, exactly", () => {
        // What the stale subscription delivered: the masked .env listing,
        // under a path the editor has no saveable file for.
        const masked = "# Environment variables\nTest=****alue\n";
        expect(nextDrafts({}, ".env", masked, undefined)).toEqual({});
        // And the same for a file deleted while the editor still held it.
        expect(nextDrafts({}, "agents/gone.py", "orphan text", undefined)).toEqual({});
    });

    it("ignores an echo of the file's own saved content", () => {
        // A file switch pushes the incoming file's text through onChange. That
        // is not an edit, and must not mark the file dirty.
        const prev = {};
        const next = nextDrafts(
            prev,
            "all_events_entry_point.py",
            FILES["all_events_entry_point.py"],
            stored("all_events_entry_point.py"),
        );
        expect(next).toBe(prev); // same identity: React skips the re-render
    });

    it("clears the dirty mark when an edit is typed back to the saved text", () => {
        const prev = { "all_events_entry_point.py": "half-typed" };
        const next = nextDrafts(
            prev,
            "all_events_entry_point.py",
            FILES["all_events_entry_point.py"],
            stored("all_events_entry_point.py"),
        );
        expect(next).toEqual({});
        expect("all_events_entry_point.py" in next).toBe(false);
    });

    it("leaves other files' drafts untouched", () => {
        const prev = { "agents/a.py": "draft a" };
        const next = nextDrafts(prev, "all_events_entry_point.py", "edited", stored("all_events_entry_point.py"));
        expect(next).toEqual({ "agents/a.py": "draft a", "all_events_entry_point.py": "edited" });
    });

    it("is a no-op when the value is already the recorded draft", () => {
        const prev = { "all_events_entry_point.py": "edited" };
        expect(nextDrafts(prev, "all_events_entry_point.py", "edited", stored("all_events_entry_point.py"))).toBe(prev);
    });
});
