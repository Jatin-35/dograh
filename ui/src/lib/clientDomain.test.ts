import { describe, expect, it } from "vitest";

import { clientRedirectTarget, isClientDomain, readCookie, subjectOfAccessToken } from "./clientDomain";

const URLS = {
    adminUrl: "https://admin-voice.botrixai.com",
    appUrl: "https://voice-app.botrixai.com",
    clientUrl: "https://voicedashboard.botrixai.com",
};
const KAILASH = "8c7d58be-ca97-49d2-b67d-5abfd8310963";
const at = (hostname: string, extra: Partial<Parameters<typeof clientRedirectTarget>[0]> = {}) =>
    clientRedirectTarget({
        hostname,
        pathname: "/workflow/7",
        search: "?tab=runs",
        userId: KAILASH,
        isSuperuser: false,
        impersonatedUserId: null,
        ...URLS,
        ...extra,
    });

describe("clientRedirectTarget", () => {
    it("sends a client on the admin domain to the client domain, same page", () => {
        expect(at("admin-voice.botrixai.com")).toBe("https://voicedashboard.botrixai.com/workflow/7?tab=runs");
    });

    it("sends a client who signed in on the app domain themselves to the client domain", () => {
        expect(at("voice-app.botrixai.com")).toBe("https://voicedashboard.botrixai.com/workflow/7?tab=runs");
    });

    it("keeps a superuser impersonating this client on the app domain", () => {
        expect(at("voice-app.botrixai.com", { impersonatedUserId: KAILASH })).toBeNull();
    });

    it("does not let a marker for another account keep a client on the app domain", () => {
        expect(at("voice-app.botrixai.com", { impersonatedUserId: "someone-else" })).toContain("voicedashboard");
    });

    it("does not let an impersonation marker keep a client on the admin domain", () => {
        expect(at("admin-voice.botrixai.com", { impersonatedUserId: KAILASH })).toContain("voicedashboard");
    });

    it("never moves a superuser", () => {
        expect(at("admin-voice.botrixai.com", { isSuperuser: true })).toBeNull();
        expect(at("voice-app.botrixai.com", { isSuperuser: true })).toBeNull();
    });

    it("leaves a client already on the client domain, and unknown hosts, alone", () => {
        expect(at("voicedashboard.botrixai.com")).toBeNull();
        expect(at("localhost")).toBeNull();
    });

    it("leaves sign-in and helper routes to run where they are", () => {
        for (const pathname of ["/handler/sign-in", "/after-sign-in", "/impersonate", "/api/x"]) {
            expect(at("admin-voice.botrixai.com", { pathname })).toBeNull();
        }
    });

    it("does nothing on single-domain installs", () => {
        expect(at("admin-voice.botrixai.com", { clientUrl: undefined })).toBeNull();
        expect(at("admin-voice.botrixai.com", { clientUrl: "not a url" })).toBeNull();
    });
});

describe("helpers", () => {
    it("reads the account from an access token", () => {
        const payload = btoa(JSON.stringify({ sub: KAILASH })).replace(/=+$/, "");
        expect(subjectOfAccessToken(`h.${payload}.s`)).toBe(KAILASH);
        expect(subjectOfAccessToken("garbage")).toBeNull();
    });

    it("reads one cookie", () => {
        expect(readCookie("botrix-impersonating", `a=1; botrix-impersonating=${KAILASH}; b=2`)).toBe(KAILASH);
        expect(readCookie("botrix-impersonating", "a=1")).toBeNull();
    });
});

describe("the client domain never shows superadmin screens", () => {
    const onClient = (extra: Partial<Parameters<typeof clientRedirectTarget>[0]> = {}) =>
        at("voicedashboard.botrixai.com", extra);

    it("moves a superuser on the client domain to the admin domain, same page", () => {
        expect(onClient({ isSuperuser: true })).toBe("https://admin-voice.botrixai.com/workflow/7?tab=runs");
        expect(onClient({ isSuperuser: true, pathname: "/superadmin", search: "" })).toBe(
            "https://admin-voice.botrixai.com/superadmin",
        );
    });

    it("sends anyone else opening a superadmin screen there to their dashboard", () => {
        expect(onClient({ pathname: "/superadmin", search: "" })).toBe("https://voicedashboard.botrixai.com/overview");
        expect(onClient({ pathname: "/superadmin/wallets/5", search: "" })).toBe(
            "https://voicedashboard.botrixai.com/overview",
        );
    });

    it("leaves a client's own screens and sign-in alone", () => {
        expect(onClient()).toBeNull();
        expect(onClient({ pathname: "/overview" })).toBeNull();
        expect(onClient({ isSuperuser: true, pathname: "/handler/sign-in" })).toBeNull();
    });

    it("keeps a superuser where they are if no admin domain is configured", () => {
        expect(onClient({ isSuperuser: true, adminUrl: undefined })).toBeNull();
    });
});

describe("isClientDomain", () => {
    it("matches only the client dashboard host", () => {
        expect(isClientDomain("voicedashboard.botrixai.com", URLS.clientUrl)).toBe(true);
        expect(isClientDomain("admin-voice.botrixai.com", URLS.clientUrl)).toBe(false);
        expect(isClientDomain("voicedashboard.botrixai.com", undefined)).toBe(false);
    });
});
