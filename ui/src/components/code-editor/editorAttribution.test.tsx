/**
 * The bug that shipped, reproduced at the component level.
 *
 * `.env`'s masked listing was saved as the body of all_events_entry_point.py.
 * The cause was not rendering, it was attribution: @monaco-editor/react
 * re-subscribes its onChange handler in an effect declared *after* the effects
 * that swap the model and push the new file's text into it, so for one commit
 * the live subscription still holds the previous render's closure. Any content
 * event escaping the library's suppression window in that commit is filed
 * against the file the user just navigated away from.
 *
 * `codeEditor.test.ts` pins the pure rules. This file pins the thing those
 * rules exist to prevent, by driving the actual failure sequence: a change
 * event delivered through a *stale* handler while the editor's model is
 * already the new file. A unit test on nextDrafts alone would keep passing if
 * someone rewired the component to attribute by the React prop again — which
 * is precisely the mistake that caused this.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { describe, expect, it } from "vitest";

import { type CodeFile, nextDrafts, pathFromModelUri } from "@/lib/codeEditor";

const ENV_FILE_PATH = ".env";
const ENTRY_POINT = "all_events_entry_point.py";
const SCHEMA = "function_definitions/check.json";

const FILES: CodeFile[] = [
    { path: ENTRY_POINT, content: "def all_events_handler(event, context):\n    pass\n" },
    { path: SCHEMA, content: '{\n  "name": "check"\n}\n' },
];

/** The masked listing the virtual .env view renders. Not valid Python — which
 * is how it announced itself, as "a syntax error on line 5". */
const ENV_VIEW = "# Environment variables\nTest=****alue\n";

/**
 * Stands in for the editor, reproducing the one behaviour that matters: its
 * change subscription lags one render behind its props, exactly as
 * @monaco-editor/react's does. `emit` fires through that stale subscription.
 */
function LaggingEditor({
    path,
    value,
    onChange,
    emitText,
}: {
    path: string;
    value: string;
    onChange: (value: string) => void;
    emitText: string | null;
}) {
    const subscribed = useRef<((v: string) => void) | null>(null);
    const modelPath = useRef<string>(path);

    // Effect order mirrors the library: the model swaps first…
    useEffect(() => {
        modelPath.current = path;
    }, [path]);
    // …and the subscription is replaced only afterwards, so during the commit
    // above it is still the previous render's closure.
    useEffect(() => {
        subscribed.current = onChange;
    });

    return (
        <>
            <span data-testid="model-path">{modelPath.current}</span>
            <button
                type="button"
                data-testid="emit"
                onClick={() => subscribed.current?.(emitText ?? value)}
            />
        </>
    );
}

/** The page's attribution logic, isolated: drafts are keyed by what actually
 * produced the text, never by the React prop. */
function Harness({ initialPath }: { initialPath: string }) {
    const [activePath, setActivePath] = useState(initialPath);
    const [drafts, setDrafts] = useState<Record<string, string>>({});
    const [emitText, setEmitText] = useState<string | null>(null);
    const filesRef = useRef<CodeFile[]>(FILES);

    // Stands in for Monaco's model registry: a real file has a model; the
    // virtual .env never gets one, which is the fix that removed the race.
    const modelUriFor = (path: string) =>
        path === ENV_FILE_PATH ? null : { path };

    const editorModelUri = useRef<{ path: string } | null>(null);
    editorModelUri.current = modelUriFor(activePath);

    const recordEdit = useCallback((value: string) => {
        const path = pathFromModelUri(editorModelUri.current);
        if (!path) return;
        const stored = filesRef.current.find((f) => f.path === path)?.content;
        setDrafts((prev) => nextDrafts(prev, path, value, stored));
    }, []);

    const activeContent =
        activePath === ENV_FILE_PATH
            ? ENV_VIEW
            : (drafts[activePath] ??
              filesRef.current.find((f) => f.path === activePath)?.content ??
              "");

    return (
        <>
            <LaggingEditor
                path={activePath}
                value={activeContent}
                onChange={recordEdit}
                emitText={emitText}
            />
            <span data-testid="drafts">{JSON.stringify(drafts)}</span>
            <span data-testid="dirty">{Object.keys(drafts).sort().join(",")}</span>
            <button data-testid="type" onClick={() => setEmitText("EDITED BY THE USER")} />
            {[ENV_FILE_PATH, ...FILES.map((f) => f.path)].map((p) => (
                <button
                    key={p}
                    data-testid={`open-${p}`}
                    onClick={() => {
                        setActivePath(p);
                        // Opening a file stops replaying the typed text: from
                        // here the editor emits that file's own content, which
                        // is what a real switch does.
                        setEmitText(null);
                    }}
                />
            ))}
        </>
    );
}

const drafts = (): Record<string, string> =>
    JSON.parse(screen.getByTestId("drafts").textContent || "{}");
const dirty = () => screen.getByTestId("dirty").textContent;
const open = (path: string) => fireEvent.click(screen.getByTestId(`open-${path}`));
const emit = () => fireEvent.click(screen.getByTestId("emit"));

describe("draft attribution across a file switch", () => {
    it("never writes the .env view into a real file's draft", () => {
        render(<Harness initialPath={ENTRY_POINT} />);

        // Navigate to .env, then fire the content event the way the library
        // does — through a subscription still holding the previous render's
        // closure, carrying the .env text.
        open(ENV_FILE_PATH);
        emit();

        // The exact production failure: .env's text under the router's name.
        expect(drafts()[ENTRY_POINT]).toBeUndefined();
        expect(JSON.stringify(drafts())).not.toContain("Test=****alue");
        expect(dirty()).toBe("");
    });

    it("never writes one real file's text into another's draft", () => {
        // The residual risk of the same root cause: not specific to .env.
        render(<Harness initialPath={ENTRY_POINT} />);

        open(SCHEMA);
        emit();

        expect(drafts()[ENTRY_POINT]).toBeUndefined();
        expect(dirty()).toBe("");
    });

    it("does not mark a file dirty merely for opening it", () => {
        render(<Harness initialPath={ENTRY_POINT} />);

        open(SCHEMA);
        emit();
        open(ENTRY_POINT);
        emit();

        expect(dirty()).toBe("");
    });

    it("still records a genuine edit, against the file being edited", () => {
        render(<Harness initialPath={ENTRY_POINT} />);

        open(SCHEMA);
        fireEvent.click(screen.getByTestId("type"));
        emit();

        expect(drafts()[SCHEMA]).toBe("EDITED BY THE USER");
        expect(drafts()[ENTRY_POINT]).toBeUndefined();
        expect(dirty()).toBe(SCHEMA);
    });

    it("keeps an edit attributed correctly after navigating away and back", () => {
        // The edited file must still be the only dirty one once the user has
        // moved through the other files — each of those visits fires the same
        // stale-subscription event that caused the original bug.
        render(<Harness initialPath={ENTRY_POINT} />);

        open(SCHEMA);
        fireEvent.click(screen.getByTestId("type"));
        emit();

        open(ENTRY_POINT);
        emit();
        open(ENV_FILE_PATH);
        emit();
        open(SCHEMA);

        expect(dirty()).toBe(SCHEMA);
        expect(drafts()[SCHEMA]).toBe("EDITED BY THE USER");
    });
});
