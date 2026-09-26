/**
 * The white-labelling contract.
 *
 * Two things are easy to undo by accident and expensive to notice:
 *
 *  - a new "Learn more" link written as a literal URL at its call site, which
 *    is invisible to the one variable that is supposed to control all of them.
 *    Eleven links had already drifted that way before this was centralised.
 *  - a link to another company's Terms of Service or Privacy Policy inside
 *    this product. That is not a branding blemish: those are not the terms the
 *    customer agreed to.
 *
 * The sweep at the bottom is the real guard — the unit tests above only prove
 * the helpers work.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
    brandDocsLink,
    CONTACT_URL,
    docsUrl,
    PRIVACY_POLICY_URL,
    TERMS_OF_SERVICE_URL,
} from "./brand";
import * as documentation from "./documentation";

describe("brand URLs", () => {
    it("builds a docs URL without doubling or dropping the slash", () => {
        expect(docsUrl("voice-agent/agent")).toMatch(/^https?:\/\/[^/]+\/voice-agent\/agent$/);
        expect(docsUrl("/voice-agent/agent")).toBe(docsUrl("voice-agent/agent"));
    });

    it("keeps an anchor intact", () => {
        expect(docsUrl("configurations/api-keys#service-keys")).toMatch(/#service-keys$/);
    });

    it("points legal pages at the company site, not the docs site", () => {
        for (const url of [PRIVACY_POLICY_URL, TERMS_OF_SERVICE_URL, CONTACT_URL]) {
            expect(url).toMatch(/^https?:\/\//);
        }
        expect(TERMS_OF_SERVICE_URL).toMatch(/terms-of-service$/);
        expect(PRIVACY_POLICY_URL).toMatch(/privacy-policy$/);
    });

    it("every exported documentation link goes through the configurable base", () => {
        // If someone adds a constant here with a hardcoded host, the switch
        // stops covering it — silently, since the link still works.
        const base = docsUrl("");
        const urls = Object.values(documentation).flatMap((value) =>
            typeof value === "string" ? [value] : Object.values(value as Record<string, string>),
        );
        expect(urls.length).toBeGreaterThan(15);
        for (const url of urls) {
            expect(url.startsWith(base)).toBe(true);
        }
    });

    it("sends backend-supplied upstream docs links to our docs, same page", () => {
        // Node and telephony-provider specs still carry docs.dograh.com links.
        expect(brandDocsLink("https://docs.dograh.com/integrations/tuner")).toBe(
            docsUrl("integrations/tuner"),
        );
        expect(brandDocsLink("https://docs.dograh.com/integrations/telephony/voicelink")).toBe(
            docsUrl("integrations/telephony/voicelink"),
        );
    });

    it("leaves every other link alone", () => {
        for (const url of [
            "https://docs.inworld.ai/tts/tts",
            "https://docs.dograh.company/x",
            docsUrl("integrations/telephony/tata-smartflo"),
        ]) {
            expect(brandDocsLink(url)).toBe(url);
        }
        expect(brandDocsLink(null)).toBeUndefined();
        expect(brandDocsLink(undefined)).toBeUndefined();
    });
});

/** Every .ts/.tsx under src, excluding generated client code and tests. */
function sourceFiles(dir: string, found: string[] = []): string[] {
    for (const entry of readdirSync(dir)) {
        const path = join(dir, entry);
        if (statSync(path).isDirectory()) {
            if (entry === "node_modules" || entry === "client") continue;
            sourceFiles(path, found);
        } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
            found.push(path);
        }
    }
    return found;
}

describe("no brand URL is hardcoded at a call site", () => {
    const SRC = join(process.cwd(), "src");
    const CONSTANTS = join(SRC, "constants");

    it("no file outside src/constants links to a documentation host", () => {
        const offenders: string[] = [];
        for (const file of sourceFiles(SRC)) {
            if (file.startsWith(CONSTANTS)) continue;
            const text = readFileSync(file, "utf8");
            // A docs link written as a literal, rather than imported.
            if (/["'`]https?:\/\/docs\.[a-z0-9.-]+/i.test(text)) {
                offenders.push(file.slice(SRC.length + 1));
            }
        }
        expect(
            offenders,
            "import the link from @/constants/documentation instead — a literal here " +
                "is invisible to NEXT_PUBLIC_DOCS_URL",
        ).toEqual([]);
    });

    it("no file links to another company's terms, privacy policy or contact page", () => {
        const offenders: string[] = [];
        for (const file of sourceFiles(SRC)) {
            if (file.startsWith(CONSTANTS)) continue;
            const text = readFileSync(file, "utf8");
            if (/["'`]https?:\/\/[^"'`]*\/(terms-of-service|privacy-policy|contact)\b/i.test(text)) {
                offenders.push(file.slice(SRC.length + 1));
            }
        }
        expect(
            offenders,
            "legal links must come from @/constants/brand — a customer clicking " +
                "Terms of Service must not land on another company's terms",
        ).toEqual([]);
    });
});
