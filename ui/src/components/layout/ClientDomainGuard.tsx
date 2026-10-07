"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import {
    arrivedByHop,
    clientRedirectTarget,
    IMPERSONATION_COOKIE,
    readCookie,
    withHop,
    withoutHop,
} from "@/lib/clientDomain";

import { userKeyOf, useSuperuserStatus } from "./useIsSuperuser";

/** Sends a signed-in user to the domain meant for them (see
 * clientRedirectTarget). A page reached through such a redirect never
 * redirects again: if it would, this browser is signed in as different
 * accounts on the two domains, so it says so (covering the page) instead of
 * bouncing between them. */
export function ClientDomainGuard() {
    const { user, loading: authLoading, getAccessToken } = useAuth();
    const { checked, isSuperuser } = useSuperuserStatus(user, authLoading, getAccessToken);
    const userId = userKeyOf(user);
    const [blocked, setBlocked] = useState(false);

    useEffect(() => {
        // Only once the server said who this is: never move a superuser
        // during the moment before the answer arrives.
        if (!userId || !checked) {
            setBlocked(false);
            return;
        }
        const hopped = arrivedByHop(window.location.search);
        const target = clientRedirectTarget({
            hostname: window.location.hostname,
            pathname: window.location.pathname,
            search: withoutHop(window.location.search),
            userId,
            isSuperuser,
            impersonatedUserId: readCookie(IMPERSONATION_COOKIE, document.cookie),
            adminUrl: process.env.NEXT_PUBLIC_ADMIN_URL,
            appUrl: process.env.NEXT_PUBLIC_APP_URL,
            clientUrl: process.env.NEXT_PUBLIC_CLIENT_URL,
        });
        if (!target) {
            setBlocked(false);
            if (hopped) {
                // Arrived fine: drop the marker from the address bar.
                const clean = `${window.location.pathname}${withoutHop(window.location.search)}${window.location.hash}`;
                window.history.replaceState(window.history.state, "", clean);
            }
            return;
        }
        if (hopped) {
            setBlocked(true);
            return;
        }
        window.location.replace(withHop(target));
    }, [userId, checked, isSuperuser]);

    if (!blocked) return null;
    return (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-background p-6">
            <div className="max-w-md space-y-4 text-center">
                <h1 className="text-lg font-semibold">This page is signed in as a different account</h1>
                <p className="text-sm text-muted-foreground">
                    This browser is signed in as one account here and as another on a different BotrixAI address. Sign
                    out here, then sign in with the account you want to use.
                </p>
                <Button onClick={() => window.location.assign("/handler/sign-out")}>Sign out</Button>
            </div>
        </div>
    );
}
