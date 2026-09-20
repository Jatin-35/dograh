"use client";

import { useEffect, useRef, useState } from 'react';

import { getAuthUserApiV1UserAuthUserGet } from '@/client/sdk.gen';
import { CallDashboard } from '@/components/call-dashboard/CallDashboard';
import { QuickActions } from '@/components/overview/QuickActions';
import SpinLoader from '@/components/SpinLoader';
import { WalletBalanceCard } from '@/components/wallet/WalletBalanceCard';
import { useAuth } from '@/lib/auth';

// Genuine client logins land on NEXT_PUBLIC_CLIENT_URL's domain (as opposed to
// the superadmin domain, or the impersonation domain a superadmin uses to
// build a client's workflow). Read via effect so server-rendered and
// first-paint client output match — `resolved` lets the caller hold off
// rendering until the real value is known, instead of flashing the admin tools
// (and making a wasted superuser check) before correcting to the client view.
// Note: this only resolves correctly on the real deployed domains — on
// localhost (or any hostname that isn't one of the three configured domains)
// it always resolves to the admin view, regardless of the test account's
// actual permissions.
function useIsOnClientDashboardDomain() {
    const [isOnClientDashboardDomain, setIsOnClientDashboardDomain] = useState(false);
    const [resolved, setResolved] = useState(false);
    useEffect(() => {
        const clientUrl = process.env.NEXT_PUBLIC_CLIENT_URL;
        if (clientUrl) {
            try {
                setIsOnClientDashboardDomain(window.location.hostname === new URL(clientUrl).hostname);
            } catch {
                // Malformed NEXT_PUBLIC_CLIENT_URL — treat as not on the client dashboard domain.
            }
        }
        setResolved(true);
    }, []);
    return { isOnClientDashboardDomain, resolved };
}

function useIsSuperuser() {
    const { user, loading: authLoading, getAccessToken } = useAuth();
    const [isSuperuser, setIsSuperuser] = useState(false);
    const hasFetched = useRef(false);
    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        (async () => {
            const accessToken = await getAccessToken();
            const response = await getAuthUserApiV1UserAuthUserGet({
                headers: { Authorization: `Bearer ${accessToken}` },
            });
            if (response.data?.is_superuser) {
                setIsSuperuser(true);
            }
        })();
    }, [authLoading, user, getAccessToken]);
    return isSuperuser;
}

// Only rendered on the admin/impersonation domains, so the superuser check is
// never made for a client.
function AdminQuickActions() {
    const isSuperuser = useIsSuperuser();
    return <QuickActions showSuperAdmin={isSuperuser} />;
}

// One Overview for everyone: the call dashboard (KPIs and charts) for the
// organization the viewer has selected. A client sees their own; a superadmin
// sees whichever client they have switched to. The people who build agents also
// get a Quick actions row above it — clients never do, and the wallet card is
// part of a client's Home only.
export default function OverviewPage() {
    const { user } = useAuth();
    const { isOnClientDashboardDomain, resolved } = useIsOnClientDashboardDomain();

    if (!resolved) {
        return <SpinLoader />;
    }

    const firstName = user?.displayName?.split(' ')[0];

    return (
        <div className="container mx-auto max-w-7xl space-y-6 px-4 py-8">
            <div className="space-y-1">
                <h1 className="text-2xl font-semibold tracking-tight">
                    Welcome{firstName ? `, ${firstName}` : ''}!
                </h1>
                <p className="text-sm text-muted-foreground">
                    {isOnClientDashboardDomain
                        ? 'Here is how your voice agents are performing.'
                        : 'Here is how the voice agents of the organization you have selected are performing.'}
                </p>
            </div>

            {!isOnClientDashboardDomain && <AdminQuickActions />}

            <CallDashboard showTestCallsSwitch={!isOnClientDashboardDomain} />

            {isOnClientDashboardDomain && (
                <div className="max-w-md">
                    <WalletBalanceCard linkHref="/billing" />
                </div>
            )}
        </div>
    );
}
