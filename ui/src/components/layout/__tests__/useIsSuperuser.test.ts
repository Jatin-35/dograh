/**
 * A superuser signed in, then a client signed in on the same tab: the client
 * kept the Super Admin item and the full menu, because the check ran once per
 * tab (Kailash, Think Gas, 7 Oct 2026). The server never granted access, but
 * the menu must follow whoever is signed in now.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getAuthUser = vi.hoisted(() => vi.fn());
vi.mock("@/client/sdk.gen", () => ({
    getAuthUserApiV1UserAuthUserGet: (...a: unknown[]) => getAuthUser(...a),
}));

import { useIsSuperuser } from "../useIsSuperuser";

const superuser = { id: "jatin" };
const client = { id: "kailash" };
const tokenFor = (user: { id: string }) => async () => `token-${user.id}`;

beforeEach(() => {
    getAuthUser.mockReset();
    getAuthUser.mockImplementation(async ({ headers }: { headers: { Authorization: string } }) => ({
        data: { is_superuser: headers.Authorization === "Bearer token-jatin" },
    }));
});

describe("useIsSuperuser", () => {
    it("drops the Super Admin menu when a client signs in on the same tab", async () => {
        const { result, rerender } = renderHook(
            ({ user, getToken }) => useIsSuperuser(user, false, getToken),
            { initialProps: { user: superuser, getToken: tokenFor(superuser) } },
        );
        await waitFor(() => expect(result.current).toBe(true));

        rerender({ user: client, getToken: tokenFor(client) });
        expect(result.current).toBe(false); // hidden at once, not after the check
        await waitFor(() => expect(getAuthUser).toHaveBeenCalledTimes(2));
        expect(result.current).toBe(false);
    });

    it("asks once per user, not on every render", async () => {
        const getToken = tokenFor(superuser);
        const { result, rerender } = renderHook(({ user }) => useIsSuperuser(user, false, getToken), {
            initialProps: { user: { ...superuser } },
        });
        await waitFor(() => expect(result.current).toBe(true));
        rerender({ user: { ...superuser } }); // new object, same person
        rerender({ user: { ...superuser } });
        expect(getAuthUser).toHaveBeenCalledTimes(1);
        expect(result.current).toBe(true);
    });

    it("ignores a superuser answer that lands after a client signed in", async () => {
        let release!: () => void;
        getAuthUser.mockImplementationOnce(
            () =>
                new Promise((resolve) => {
                    release = () => resolve({ data: { is_superuser: true } });
                }),
        );
        const { result, rerender } = renderHook(
            ({ user, getToken }) => useIsSuperuser(user, false, getToken),
            { initialProps: { user: superuser, getToken: tokenFor(superuser) } },
        );
        await waitFor(() => expect(getAuthUser).toHaveBeenCalledTimes(1));
        rerender({ user: client, getToken: tokenFor(client) });
        await waitFor(() => expect(getAuthUser).toHaveBeenCalledTimes(2));
        release(); // the superuser's slow answer arrives now
        await new Promise((r) => setTimeout(r, 0));
        expect(result.current).toBe(false);
    });

    it("is false when signed out or while auth is loading", () => {
        expect(renderHook(() => useIsSuperuser(null, false, tokenFor(superuser))).result.current).toBe(false);
        expect(renderHook(() => useIsSuperuser(superuser, true, tokenFor(superuser))).result.current).toBe(false);
        expect(getAuthUser).not.toHaveBeenCalled();
    });
});
