/**
 * The embed widget's public contract.
 *
 * Renaming this widget is the one change in the codebase that can break sites
 * we do not control. The script is pasted into customer pages, and those pages
 * are not going to be edited when we rename something — so every name that was
 * ever public has to keep working.
 *
 * Three earlier rebrands got this wrong in the *other* direction: the docs were
 * renamed while the code was not, so `pip install botrixai-sdk`,
 * `botrixai-widget.js` and `botrixai-inline-container` were all published as
 * instructions that could not work. These tests pin the code side, so the docs
 * have something true to describe.
 *
 * @vitest-environment jsdom
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it } from "vitest";

const WIDGET = join(process.cwd(), "public/embed/botrixai-widget.js");
const SHIM = join(process.cwd(), "public/embed/dograh-widget.js");

/** The widget is a plain script, not a module, so nothing in `src/` declares
 * these. Typed loosely on purpose: the point of these tests is to assert what
 * the *runtime* object carries, not to restate a shape TypeScript would then
 * take on trust. */
type WidgetApi = Record<string, unknown>;
declare global {
    interface Window {
        BotrixAIWidget?: WidgetApi;
        DograhWidget?: WidgetApi;
    }
}

/** Run the widget IIFE against the current jsdom document. */
function loadWidget() {
    // The widget reads its own <script src> for the token, so give it one.
    const tag = document.createElement("script");
    tag.src = "https://example.test/embed/botrixai-widget.js?token=t&environment=test";
    document.head.appendChild(tag);
    // eslint-disable-next-line no-new-func
    new Function(readFileSync(WIDGET, "utf8"))();
}

describe("the widget's public global", () => {
    beforeEach(() => {
        document.head.innerHTML = "";
        document.body.innerHTML = "";
        delete window.BotrixAIWidget;
        delete window.DograhWidget;
    });

    it("exposes window.BotrixAIWidget", () => {
        loadWidget();
        expect(window.BotrixAIWidget).toBeDefined();
    });

    it("still exposes window.DograhWidget, and as the SAME object", () => {
        // A copy would silently break callbacks: a customer registering
        // through the old name would attach to an object the widget never
        // reads. Identity is the property that matters, not mere presence.
        loadWidget();
        expect(window.DograhWidget).toBe(window.BotrixAIWidget);
    });

    it("keeps every method the documentation promises", () => {
        loadWidget();
        for (const method of [
            "init", "start", "stop", "end", "retry", "open", "close",
            "getState", "isInlineMode", "refresh", "initInline",
            "onReady", "onCallStart", "onCallConnected", "onCallDisconnected",
            "onCallEnd", "onError", "onStatusChange",
        ]) {
            expect(
                typeof window.BotrixAIWidget![method],
                `add-to-website.mdx documents ${method}()`,
            ).toBe("function");
        }
    });
});

describe("the compatibility shim at the old path", () => {
    it("forwards to the new filename, keeping the query string", () => {
        document.head.innerHTML = "";
        const tag = document.createElement("script");
        tag.src = "https://example.test/embed/dograh-widget.js?token=abc123&environment=production";
        document.head.appendChild(tag);
        Object.defineProperty(document, "currentScript", {
            value: tag,
            configurable: true,
        });

        // eslint-disable-next-line no-new-func
        new Function(readFileSync(SHIM, "utf8"))();

        const injected = [...document.head.querySelectorAll("script")].find((s) =>
            s.src.includes("botrixai-widget.js"),
        );
        expect(injected, "the shim did not inject the renamed script").toBeDefined();
        // The token is how the widget authenticates. Dropping the query string
        // would leave every pre-rename embed loading a widget with no token.
        expect(injected!.src).toContain("token=abc123");
        expect(injected!.src).toContain("environment=production");
    });
});

describe("no other company's identifiers ship in the widget", () => {
    const source = readFileSync(WIDGET, "utf8");

    it("does not call another company's API", () => {
        expect(source).not.toContain("api.dograh.com");
    });

    it("only mentions the old name where back-compat requires it", () => {
        // Every remaining occurrence must be one of the three deliberate
        // fallbacks. A new one means a rename was missed.
        const lines = source
            .split("\n")
            .filter((l) => /dograh/i.test(l))
            .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)); // ignore comments

        for (const line of lines) {
            expect(
                /window\.DograhWidget = window\.BotrixAIWidget|dograh-inline-container|data-dograh-context|dograh-widget\.js/.test(
                    line,
                ),
                `unexpected Dograh reference: ${line.trim()}`,
            ).toBe(true);
        }
    });
});
