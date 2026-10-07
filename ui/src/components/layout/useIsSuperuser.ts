import { useEffect, useRef, useState } from "react";

import { getAuthUserApiV1UserAuthUserGet } from "@/client/sdk.gen";

/** Who is signed in, as a stable string (the user object itself is not). */
export function userKeyOf(user: unknown): string | null {
    if (!user) return null;
    const u = user as { id?: unknown; primaryEmail?: unknown; email?: unknown };
    return String(u.id ?? u.primaryEmail ?? u.email ?? "");
}

export interface SuperuserStatus {
    /** True once the server answered for the signed-in user. */
    checked: boolean;
    isSuperuser: boolean;
}

const UNKNOWN: SuperuserStatus = { checked: false, isSuperuser: false };

/** Whether the signed-in user is a superuser, asked of the server.
 *
 * Asked again whenever the signed-in user changes. It used to be asked once
 * per tab, so signing in as a client in a tab where a superuser had been
 * signed in kept the superuser's Super Admin item and full menu. Not a
 * superuser while asking or after a failed check (still `checked`, so callers
 * waiting on it don't wait forever); the server enforces superuser access.
 */
export function useSuperuserStatus(
    user: unknown,
    authLoading: boolean,
    getAccessToken: () => Promise<string>,
): SuperuserStatus {
    const [status, setStatusRaw] = useState<SuperuserStatus>(UNKNOWN);
    // Same values keep the same object, so setting them never re-renders.
    const setStatus = (next: SuperuserStatus) =>
        setStatusRaw((prev) =>
            prev.checked === next.checked && prev.isSuperuser === next.isSuperuser ? prev : next,
        );
    const checkedFor = useRef<string | null>(null);
    // Read through a ref: a caller passing a new function each render must
    // not restart the check.
    const getAccessTokenRef = useRef(getAccessToken);
    getAccessTokenRef.current = getAccessToken;
    const userKey = userKeyOf(user);

    useEffect(() => {
        if (authLoading || !userKey) {
            checkedFor.current = null;
            setStatus(UNKNOWN);
            return;
        }
        if (checkedFor.current === userKey) return;
        checkedFor.current = userKey;
        setStatus(UNKNOWN);
        (async () => {
            let isSuperuser = false;
            try {
                const accessToken = await getAccessTokenRef.current();
                const response = await getAuthUserApiV1UserAuthUserGet({
                    headers: { Authorization: `Bearer ${accessToken}` },
                });
                isSuperuser = Boolean(response.data?.is_superuser);
            } catch {
                // Treated as not a superuser.
            }
            // Ignore an answer that arrives after someone else signed in.
            if (checkedFor.current === userKey) {
                setStatus({ checked: true, isSuperuser });
            }
        })();
    }, [authLoading, userKey]);

    return status;
}

export function useIsSuperuser(
    user: unknown,
    authLoading: boolean,
    getAccessToken: () => Promise<string>,
): boolean {
    return useSuperuserStatus(user, authLoading, getAccessToken).isSuperuser;
}
