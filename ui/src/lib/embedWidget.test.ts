/**
 * The embed widget's public contract.
 *
 * The script is pasted into customer pages, so the names it exposes are a
 * public API: the script path, `window.BotrixAIWidget`, the
 * `data-botrixai-context` attribute and the `botrixai-inline-container` id.
 * These tests pin the code side, so the docs have something true to describe.
 *
 * @vitest-environment jsdom
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const WIDGET = join(process.cwd(), "public/embed/botrixai-widget.js");

/** The widget is a plain script, not a module, so nothing in `src/` declares
 * these. Typed loosely on purpose: the point of these tests is to assert what
 * the *runtime* object carries, not to restate a shape TypeScript would then
 * take on trust. */
type WidgetApi = Record<string, unknown>;
declare global {
    interface Window {
        BotrixAIWidget?: WidgetApi;
    }
}

function addScriptTag(src: string, context?: Record<string, unknown>) {
    const tag = document.createElement("script");
    tag.src = src;
    if (context) tag.setAttribute("data-botrixai-context", JSON.stringify(context));
    document.head.appendChild(tag);
    // As on a real page, the running script is this tag.
    Object.defineProperty(document, "currentScript", { value: tag, configurable: true });
    return tag;
}

/** Run the widget IIFE against the current jsdom document. */
function runWidget() {
    // eslint-disable-next-line no-new-func
    new Function(readFileSync(WIDGET, "utf8"))();
}

describe("the widget's public global", () => {
    beforeEach(() => {
        document.head.innerHTML = "";
        document.body.innerHTML = "";
        delete window.BotrixAIWidget;
    });

    it("exposes window.BotrixAIWidget", () => {
        addScriptTag("https://example.test/embed/botrixai-widget.js?token=t&environment=test");
        runWidget();
        expect(window.BotrixAIWidget).toBeDefined();
    });

    it("keeps every method the documentation promises", () => {
        addScriptTag("https://example.test/embed/botrixai-widget.js?token=t&environment=test");
        runWidget();
        for (const method of [
            "init", "start", "stop", "end", "retry", "open", "close",
            "getState", "isInlineMode", "refresh", "initInline",
            "onReady", "onCallStart", "onCallConnected", "onCallDisconnected",
            "onCallEnd", "onError", "onStatusChange",
            // Visitor context and chat widgets.
            "setContext", "getContext", "isChatMode", "startChat", "sendMessage",
            "getMessages", "endChat", "onMessage", "onChatStateChange",
        ]) {
            expect(
                typeof window.BotrixAIWidget![method],
                `add-to-website.mdx documents ${method}()`,
            ).toBe("function");
        }
    });
});

describe("the documented embed code, voice and chat", () => {
    beforeEach(() => {
        document.head.innerHTML = "";
        document.body.innerHTML = '<div id="botrixai-inline-container"></div>';
        delete window.BotrixAIWidget;
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    async function loadEmbed(widgetType: "chat" | "voice") {
        addScriptTag(
            "https://example.test/embed/botrixai-widget.js?token=emb_TEST&environment=test",
            { customer_name: "Asha" },
        );
        const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
            expect(String(input)).toContain("/api/v1/public/embed/config/emb_TEST");
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    workflow_id: 7,
                    settings: { widgetType, embedMode: "inline" },
                    texts: {},
                    auto_start: false,
                }),
            } as Response;
        });
        vi.stubGlobal("fetch", fetchMock);
        runWidget();
        // init() is single-flight: this awaits the load the script started.
        await (window.BotrixAIWidget!.init as () => Promise<void>)();
        return fetchMock;
    }

    it("reads the token from the script URL and the context attribute", async () => {
        const fetchMock = await loadEmbed("chat");
        expect(fetchMock).toHaveBeenCalled();
        const getContext = window.BotrixAIWidget!.getContext as () => Record<string, unknown>;
        expect(getContext()).toMatchObject({ customer_name: "Asha" });
    });

    it("renders a chat widget inside the inline container", async () => {
        await loadEmbed("chat");
        expect((window.BotrixAIWidget!.isChatMode as () => boolean)()).toBe(true);
        expect(document.getElementById("botrixai-inline-container")!.children.length).toBeGreaterThan(0);
    });

    it("renders a voice widget inside the inline container", async () => {
        await loadEmbed("voice");
        expect((window.BotrixAIWidget!.isChatMode as () => boolean)()).toBe(false);
        expect(document.getElementById("botrixai-inline-container")!.children.length).toBeGreaterThan(0);
    });
});

describe("no other company's identifiers ship in the widget", () => {
    const source = readFileSync(WIDGET, "utf8");

    it("does not call another company's API", () => {
        expect(source).not.toContain("api.dograh.com");
    });

    it("never mentions the upstream product name", () => {
        const lines = source.split("\n").filter((l) => /dograh/i.test(l));
        expect(lines, `unexpected Dograh reference: ${lines[0]?.trim()}`).toEqual([]);
    });

    it("ships no script under the upstream name", () => {
        expect(existsSync(join(process.cwd(), "public/embed/dograh-widget.js"))).toBe(false);
    });
});
