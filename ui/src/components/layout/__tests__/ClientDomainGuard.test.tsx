/**
 * The guard end to end with a mocked server answer: a client on the admin
 * domain is moved, a superuser never is (not even before the answer arrives).
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: null as null | { id: string } }));
const getAuthUser = vi.hoisted(() => vi.fn());
vi.mock("@/lib/auth", () => ({
    useAuth: () => ({ user: auth.user, loading: false, getAccessToken: async () => "t" }),
}));
vi.mock("@/client/sdk.gen", () => ({
    getAuthUserApiV1UserAuthUserGet: (...a: unknown[]) => getAuthUser(...a),
}));

import { ClientDomainGuard } from "../ClientDomainGuard";

const replace = vi.fn();
const replaceState = vi.fn();
const setPage = (hostname: string, pathname: string, search = "") =>
    vi.stubGlobal("location", { hostname, pathname, search, hash: "", replace, assign: vi.fn() });

beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_ADMIN_URL", "https://admin-voice.botrixai.com");
    vi.stubEnv("NEXT_PUBLIC_APP_URL", "https://voice-app.botrixai.com");
    vi.stubEnv("NEXT_PUBLIC_CLIENT_URL", "https://voicedashboard.botrixai.com");
    setPage("admin-voice.botrixai.com", "/overview");
    vi.spyOn(window.history, "replaceState").mockImplementation(replaceState);
    replace.mockReset();
    replaceState.mockReset();
    getAuthUser.mockReset();
});

afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
});

describe("ClientDomainGuard", () => {
    it("moves a signed-in client off the admin domain", async () => {
        auth.user = { id: "kailash" };
        getAuthUser.mockResolvedValue({ data: { is_superuser: false } });
        render(<ClientDomainGuard />);
        await waitFor(() =>
            expect(replace).toHaveBeenCalledWith("https://voicedashboard.botrixai.com/overview?dg_hop=1"),
        );
    });

    it("never moves a superuser, including while the check is in flight", async () => {
        auth.user = { id: "jatin" };
        let answer!: (v: unknown) => void;
        getAuthUser.mockReturnValue(new Promise((r) => (answer = r)));
        render(<ClientDomainGuard />);
        await new Promise((r) => setTimeout(r, 20));
        expect(replace).not.toHaveBeenCalled(); // not before the answer
        answer({ data: { is_superuser: true } });
        await new Promise((r) => setTimeout(r, 20));
        expect(replace).not.toHaveBeenCalled();
    });

    it("does nothing when nobody is signed in", async () => {
        auth.user = null;
        render(<ClientDomainGuard />);
        await new Promise((r) => setTimeout(r, 20));
        expect(getAuthUser).not.toHaveBeenCalled();
        expect(replace).not.toHaveBeenCalled();
    });

    it("never bounces back: one browser signed in as a client on admin and a superuser on the client domain", async () => {
        // Hop 1 happened on admin-voice (Kailash). Now on voicedashboard this
        // browser is signed in as a superuser, who would be sent back.
        setPage("voicedashboard.botrixai.com", "/superadmin/agents", "?dg_hop=1");
        auth.user = { id: "jatin" };
        getAuthUser.mockResolvedValue({ data: { is_superuser: true } });
        render(<ClientDomainGuard />);
        expect(await screen.findByText("This page is signed in as a different account")).toBeTruthy();
        expect(replace).not.toHaveBeenCalled();
    });

    it("drops the marker from the address once a hop lands where it should", async () => {
        setPage("voicedashboard.botrixai.com", "/overview", "?tab=1&dg_hop=1");
        auth.user = { id: "kailash" };
        getAuthUser.mockResolvedValue({ data: { is_superuser: false } });
        render(<ClientDomainGuard />);
        await waitFor(() => expect(replaceState).toHaveBeenCalledWith(null, "", "/overview?tab=1"));
        expect(replace).not.toHaveBeenCalled();
        expect(screen.queryByText(/different account/)).toBeNull();
    });
});
