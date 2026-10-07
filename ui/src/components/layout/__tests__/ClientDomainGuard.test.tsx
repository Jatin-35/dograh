/**
 * The guard end to end with a mocked server answer: a client on the admin
 * domain is moved, a superuser never is (not even before the answer arrives).
 */
import { render, waitFor } from "@testing-library/react";
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

beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_ADMIN_URL", "https://admin-voice.botrixai.com");
    vi.stubEnv("NEXT_PUBLIC_APP_URL", "https://voice-app.botrixai.com");
    vi.stubEnv("NEXT_PUBLIC_CLIENT_URL", "https://voicedashboard.botrixai.com");
    vi.stubGlobal("location", {
        hostname: "admin-voice.botrixai.com",
        pathname: "/overview",
        search: "",
        replace,
    });
    replace.mockReset();
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
        await waitFor(() => expect(replace).toHaveBeenCalledWith("https://voicedashboard.botrixai.com/overview"));
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
});
