"use client";

import { useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { clientRedirectTarget, IMPERSONATION_COOKIE, readCookie } from "@/lib/clientDomain";

import { userKeyOf, useSuperuserStatus } from "./useIsSuperuser";

/** Sends a signed-in client on the admin or app domain to the client
 * dashboard domain, same page. See clientRedirectTarget. Renders nothing. */
export function ClientDomainGuard() {
    const { user, loading: authLoading, getAccessToken } = useAuth();
    const { checked, isSuperuser } = useSuperuserStatus(user, authLoading, getAccessToken);
    const userId = userKeyOf(user);

    useEffect(() => {
        // Only once the server said who this is: never bounce a superuser
        // during the moment before the answer arrives.
        if (!userId || !checked) return;
        const target = clientRedirectTarget({
            hostname: window.location.hostname,
            pathname: window.location.pathname,
            search: window.location.search,
            userId,
            isSuperuser,
            impersonatedUserId: readCookie(IMPERSONATION_COOKIE, document.cookie),
            adminUrl: process.env.NEXT_PUBLIC_ADMIN_URL,
            appUrl: process.env.NEXT_PUBLIC_APP_URL,
            clientUrl: process.env.NEXT_PUBLIC_CLIENT_URL,
        });
        if (target) window.location.replace(target);
    }, [userId, checked, isSuperuser]);

    return null;
}
