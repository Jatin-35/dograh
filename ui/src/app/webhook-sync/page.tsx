"use client";

import { Plus, Webhook } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AuditPanel } from "@/components/webhook-sync/AuditPanel";
import { LeadsPanel } from "@/components/webhook-sync/LeadsPanel";
import { ErrorBox, LoadingRows, WhenCell } from "@/components/webhook-sync/shared";
import { useAuth } from "@/lib/auth";
import { useOrganizationTimezone } from "@/lib/useOrganizationTimezone";
import { AUTH_TYPE_LABELS, callingStatus, listEndpoints, type WebhookEndpoint } from "@/lib/webhookSync";

const TABS = ["endpoints", "leads", "history"] as const;
type Tab = (typeof TABS)[number];

function WebhookSyncHome() {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const { user, loading: authLoading, redirectToLogin } = useAuth();
    // Whether someone is signed in, not the user object: a new object for the
    // same user (e.g. after a token refresh) must not re-run the fetches.
    const signedIn = Boolean(user);
    const timezone = useOrganizationTimezone();
    const [endpoints, setEndpoints] = useState<WebhookEndpoint[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    const tabParam = searchParams.get("tab");
    const tab: Tab = TABS.includes(tabParam as Tab) ? (tabParam as Tab) : "endpoints";

    useEffect(() => {
        if (!authLoading && !signedIn) redirectToLogin();
    }, [authLoading, signedIn, redirectToLogin]);

    useEffect(() => {
        if (authLoading || !signedIn) return;
        listEndpoints()
            .then(setEndpoints)
            .catch((e) => setError(e instanceof Error ? e.message : "Failed to load webhook endpoints"));
    }, [authLoading, signedIn]);

    const names = useMemo(() => Object.fromEntries((endpoints ?? []).map((e) => [e.id, e.name])), [endpoints]);
    const workflows = useMemo(
        () => Object.fromEntries((endpoints ?? []).map((e) => [e.id, e.workflow_id])),
        [endpoints],
    );

    const selectTab = (next: string) => {
        // A tab has its own filters and page; don't carry them over.
        router.push(`${pathname}?tab=${next}`, { scroll: false });
    };

    return (
        <div className="container mx-auto space-y-6 p-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="mb-2 text-3xl font-bold">Webhook Sync</h1>
                    <p className="text-muted-foreground">
                        Receive new leads from your CRM and have your voice agent call them.
                    </p>
                </div>
                <Button onClick={() => router.push("/webhook-sync/new")}>
                    <Plus className="mr-2 h-4 w-4" />
                    New endpoint
                </Button>
            </div>

            <Tabs value={tab} onValueChange={selectTab}>
                <TabsList>
                    <TabsTrigger value="endpoints">Endpoints</TabsTrigger>
                    <TabsTrigger value="leads">All leads</TabsTrigger>
                    <TabsTrigger value="history">History</TabsTrigger>
                </TabsList>

                <TabsContent value="endpoints" className="mt-6">
                    <Card>
                        <CardHeader>
                            <CardTitle>Endpoints</CardTitle>
                            <CardDescription>Each endpoint is one webhook URL for one CRM and one agent.</CardDescription>
                        </CardHeader>
                        <CardContent>
                            {error ? (
                                <ErrorBox message={error} />
                            ) : endpoints === null ? (
                                <LoadingRows />
                            ) : endpoints.length === 0 ? (
                                <div className="py-10 text-center">
                                    <Webhook className="mx-auto mb-3 h-10 w-10 text-muted-foreground" />
                                    <p className="mb-4 text-muted-foreground">
                                        No endpoints yet. Create one to get a webhook URL for your CRM.
                                    </p>
                                    <Button variant="outline" onClick={() => router.push("/webhook-sync/new")}>
                                        Create your first endpoint
                                    </Button>
                                </div>
                            ) : (
                                <div className="overflow-x-auto">
                                    <Table>
                                        <TableHeader>
                                            <TableRow>
                                                <TableHead>Name</TableHead>
                                                <TableHead>Agent</TableHead>
                                                <TableHead>Status</TableHead>
                                                <TableHead>Calling</TableHead>
                                                <TableHead>Authentication</TableHead>
                                                <TableHead className="text-right">Leads today</TableHead>
                                                <TableHead className="text-right">Total leads</TableHead>
                                                <TableHead>Created</TableHead>
                                            </TableRow>
                                        </TableHeader>
                                        <TableBody>
                                            {endpoints.map((endpoint) => (
                                                <TableRow
                                                    key={endpoint.id}
                                                    className="cursor-pointer hover:bg-muted/50"
                                                    onClick={() => router.push(`/webhook-sync/${endpoint.id}`)}
                                                >
                                                    <TableCell className="font-medium">{endpoint.name}</TableCell>
                                                    <TableCell className="text-sm">
                                                        {endpoint.workflow_name ?? `#${endpoint.workflow_id}`}
                                                    </TableCell>
                                                    <TableCell>
                                                        <Badge variant={endpoint.is_active ? "default" : "outline"}>
                                                            {endpoint.is_active ? "Active" : "Paused"}
                                                        </Badge>
                                                    </TableCell>
                                                    <TableCell className="text-sm" title={callingStatus(endpoint).hint}>
                                                        <span
                                                            className={
                                                                callingStatus(endpoint).tone === "warn"
                                                                    ? "font-medium text-amber-700 dark:text-amber-300"
                                                                    : callingStatus(endpoint).tone === "off"
                                                                      ? "text-muted-foreground"
                                                                      : ""
                                                            }
                                                        >
                                                            {callingStatus(endpoint).label}
                                                        </span>
                                                    </TableCell>
                                                    <TableCell className="text-sm">
                                                        {AUTH_TYPE_LABELS[endpoint.auth_type]?.label ?? endpoint.auth_type}
                                                    </TableCell>
                                                    <TableCell className="text-right tabular-nums">{endpoint.leads_today}</TableCell>
                                                    <TableCell className="text-right tabular-nums">{endpoint.leads_total}</TableCell>
                                                    <TableCell>
                                                        <WhenCell iso={endpoint.created_at} timezone={timezone} />
                                                    </TableCell>
                                                </TableRow>
                                            ))}
                                        </TableBody>
                                    </Table>
                                </div>
                            )}
                        </CardContent>
                    </Card>
                </TabsContent>

                <TabsContent value="leads" className="mt-6">
                    {tab === "leads" && <LeadsPanel endpointNames={names} endpointWorkflows={workflows} />}
                </TabsContent>

                <TabsContent value="history" className="mt-6">
                    {tab === "history" && <AuditPanel />}
                </TabsContent>
            </Tabs>
        </div>
    );
}

export default function WebhookSyncPage() {
    return (
        <Suspense fallback={<div className="container mx-auto p-6">Loading…</div>}>
            <WebhookSyncHome />
        </Suspense>
    );
}
