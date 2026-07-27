"use client";

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { getAuthUserApiV1UserAuthUserGet } from '@/client/sdk.gen';
import SpinLoader from '@/components/SpinLoader';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { useAuth } from '@/lib/auth';

// Genuine client logins land on NEXT_PUBLIC_CLIENT_URL's domain (as opposed to
// the superadmin domain, or the impersonation domain a superadmin uses to
// build a client's workflow). Read via effect so server-rendered and
// first-paint client output match — `resolved` lets the caller hold off
// rendering either overview until the real value is known, instead of
// flashing AdminOverview (with its Configure Services card and a wasted
// superuser check) before correcting to ClientOverview. Note: this only
// resolves correctly on the real deployed domains — on localhost (or any
// hostname that isn't one of the three configured domains) it always
// resolves to AdminOverview, regardless of the test account's actual
// permissions.
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

function WelcomeCard() {
    const { user } = useAuth();
    return (
        <Card className="mb-8">
            <CardHeader>
                <CardTitle className="text-3xl">
                    Welcome{user?.displayName ? `, ${user.displayName.split(' ')[0]}` : ''}!
                </CardTitle>
                <CardDescription className="text-lg mt-2">
                    Get started with building voice AI workflows
                </CardDescription>
            </CardHeader>
        </Card>
    );
}

// Client-dashboard overview: only what a client can actually act on. No
// Configure Services / Models card (that nav item is hidden for restricted
// clients elsewhere in the app), no superadmin content.
function ClientOverview() {
    return (
        <div className="container mx-auto px-4 py-8">
            <div className="max-w-4xl mx-auto">
                <WelcomeCard />

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <Card>
                        <CardHeader>
                            <CardTitle>Create and Manage your Voice Agents</CardTitle>
                            <CardDescription>
                                Build powerful AI Voice Agents with our visual editor
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild>
                                <Link href="/workflow">
                                    Go to Agents
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Track Your Agent Runs</CardTitle>
                            <CardDescription>
                                See call-by-call run history and outcomes
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild variant="outline">
                                <Link href="/usage">
                                    View Agent Runs
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Daily Reports</CardTitle>
                            <CardDescription>
                                See call volume, dispositions, and duration breakdowns
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild variant="outline">
                                <Link href="/reports">
                                    View Reports
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>View Analytics</CardTitle>
                            <CardDescription>
                                Deeper insights across your voice agents
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button variant="outline" disabled>
                                Coming Soon
                            </Button>
                        </CardContent>
                    </Card>
                </div>
            </div>
        </div>
    );
}

// Admin/dev overview (superadmin domain, or the impersonation domain used to
// build a client's workflow): dev-facing quick actions, plus a Super Admin
// card for superusers.
function AdminOverview() {
    const isSuperuser = useIsSuperuser();

    return (
        <div className="container mx-auto px-4 py-8">
            <div className="max-w-4xl mx-auto">
                <WelcomeCard />

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <Card>
                        <CardHeader>
                            <CardTitle>Create and Manage your Voice Agents</CardTitle>
                            <CardDescription>
                                Build powerful AI Voice Agents with our visual editor
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild>
                                <Link href="/workflow">
                                    Go to Agents
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Configure Services</CardTitle>
                            <CardDescription>
                                Set up your AI services like LLM, TTS, and STT providers
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild variant="outline">
                                <Link href="/model-configurations">
                                    Configure Models
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>

                    {isSuperuser && (
                        <Card>
                            <CardHeader>
                                <CardTitle>Super Admin</CardTitle>
                                <CardDescription>
                                    Browse organizations, agents, and runs across all clients
                                </CardDescription>
                            </CardHeader>
                            <CardContent>
                                <Button asChild variant="outline">
                                    <Link href="/superadmin">
                                        Go to Super Admin
                                    </Link>
                                </Button>
                            </CardContent>
                        </Card>
                    )}
                </div>
            </div>
        </div>
    );
}

export default function OverviewPage() {
    const { isOnClientDashboardDomain, resolved } = useIsOnClientDashboardDomain();

    if (!resolved) {
        return <SpinLoader />;
    }

    return isOnClientDashboardDomain ? <ClientOverview /> : <AdminOverview />;
}
