/**
 * The support corner's configuration rules.
 *
 * The regression these guard against is a real one that shipped: the image
 * hardcoded dograh-hq's Chatwoot server and website token, so clients saw
 * Dograh's branding and opened support conversations in a third party's
 * inbox. The default must be "no third-party chat", and the two affordances
 * must never both claim the corner.
 */

import { readFileSync } from "node:fs";

import { beforeEach, describe, expect, it, vi } from "vitest";

/** Re-import the module with a specific environment in place. */
async function loadWith(env: Record<string, string | undefined>) {
    vi.resetModules();
    for (const [key, value] of Object.entries(env)) {
        vi.stubEnv(key, value as string);
    }
    return import("./support");
}

beforeEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
});

describe("isChatwootConfigured", () => {
    it("is false when neither value is set — the default", async () => {
        const support = await loadWith({
            NEXT_PUBLIC_CHATWOOT_URL: "",
            NEXT_PUBLIC_CHATWOOT_TOKEN: "",
        });
        expect(support.isChatwootConfigured).toBe(false);
    });

    it.each([
        ["url only", { url: "https://chat.example.com", token: "" }],
        ["token only", { url: "", token: "abc123" }],
    ])("is false with %s — a half-configured inbox is not an inbox", async (_label, { url, token }) => {
        const support = await loadWith({
            NEXT_PUBLIC_CHATWOOT_URL: url,
            NEXT_PUBLIC_CHATWOOT_TOKEN: token,
        });
        expect(support.isChatwootConfigured).toBe(false);
    });

    it("is true only when a deployment supplies both", async () => {
        const support = await loadWith({
            NEXT_PUBLIC_CHATWOOT_URL: "https://chat.example.com",
            NEXT_PUBLIC_CHATWOOT_TOKEN: "abc123",
        });
        expect(support.isChatwootConfigured).toBe(true);
    });
});

describe("the image does not ship someone else's inbox", () => {
    // The unit tests above stub the environment, so they cannot see the place
    // this actually went wrong: `ui/Dockerfile` baked in dograh-hq's Chatwoot
    // host and website token above the build step. `NEXT_PUBLIC_*` is inlined
    // at `next build`, so that reached every client bundle and no runtime env
    // var could override it. We merge from dograh-hq/dograh, so an upstream
    // merge restoring those two lines is the likely way this regresses.
    const dockerfile = readFileSync("Dockerfile", "utf8");

    const valueOf = (name: string) => {
        const match = dockerfile.match(
            new RegExp(`^ENV ${name}=\\s*"([^"]*)"`, "m"),
        );
        return match?.[1];
    };

    it.each(["NEXT_PUBLIC_CHATWOOT_URL", "NEXT_PUBLIC_CHATWOOT_TOKEN"])(
        "leaves %s empty, so no third-party chat loads by default",
        (name) => {
            const value = valueOf(name);
            expect(
                value === undefined || value === `\${${name}}` || value === "",
                `${name} must not be hardcoded in ui/Dockerfile — it is inlined ` +
                    "into the client bundle at build time and cannot be " +
                    `overridden at runtime. Got: ${value}`,
            ).toBe(true);
        },
    );

    it("points no instruction at a dograh-hq host", () => {
        // Comments are exempt: the ones above those ARGs name the host
        // deliberately, to explain why it must not come back.
        const instructions = dockerfile
            .split("\n")
            .filter((line) => !line.trimStart().startsWith("#"));
        expect(instructions.join("\n")).not.toMatch(/chat\.dograh\.com/);
    });
});

describe("contact details", () => {
    it("are empty strings when unset, so nothing renders a placeholder", async () => {
        const support = await loadWith({
            NEXT_PUBLIC_SUPPORT_EMAIL: undefined,
            NEXT_PUBLIC_SUPPORT_PHONE: undefined,
            NEXT_PUBLIC_SUPPORT_WHATSAPP: undefined,
        });
        expect(support.SUPPORT_EMAIL).toBe("");
        expect(support.SUPPORT_PHONE).toBe("");
        expect(support.SUPPORT_WHATSAPP).toBe("");
    });

    it("are trimmed, so a stray newline in compose doesn't break the link", async () => {
        const support = await loadWith({
            NEXT_PUBLIC_SUPPORT_EMAIL: "  help@example.com \n",
        });
        expect(support.SUPPORT_EMAIL).toBe("help@example.com");
    });
});

describe("whatsappHref", () => {
    it.each([
        ["+91 98765 43210", "https://wa.me/919876543210"],
        ["+1 (555) 010-9999", "https://wa.me/15550109999"],
        ["919876543210", "https://wa.me/919876543210"],
    ])("strips punctuation from %s", async (input, expected) => {
        const { whatsappHref } = await import("./support");
        expect(whatsappHref(input)).toBe(expected);
    });
});

describe("hidesSupportLauncher", () => {
    it.each([
        "/workflow/42",
        "/workflow/42/run/7",
        "/code-editor",
        "/code-editor/files",
    ])("hides on %s, whose bottom-right corner is already spoken for", async (pathname) => {
        const { hidesSupportLauncher } = await import("./support");
        expect(hidesSupportLauncher(pathname)).toBe(true);
    });

    it.each([
        "/",
        "/workflow",
        "/workflow/create",
        "/overview",
        "/tools",
        "/settings",
        // Not the code editor — a different route that merely starts the same way.
        "/code-editors",
    ])("keeps the launcher on %s", async (pathname) => {
        const { hidesSupportLauncher } = await import("./support");
        expect(hidesSupportLauncher(pathname)).toBe(false);
    });
});
