"use client";

import { ArrowLeft, Loader2, Wallet } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { listOrganizationsApiV1SuperuserOrganizationsGet } from "@/client/sdk.gen";
import type { SuperuserOrganizationResponse } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

export default function SuperadminWalletsPage() {
    const { user, loading: authLoading } = useAuth();
    const [organizations, setOrganizations] = useState<SuperuserOrganizationResponse[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    // "managed" = orgs provisioned via the panel (they carry a contact
    // email); "all" = every org on the platform, incl. auto-created / legacy.
    const [filterMode, setFilterMode] = useState<"managed" | "all">("managed");
    const managedCount = organizations.filter((o) => !!o.primary_contact_email).length;
    const visibleOrganizations =
        filterMode === "managed"
            ? organizations.filter((o) => !!o.primary_contact_email)
            : organizations;

    const fetchOrganizations = useCallback(async () => {
        setIsLoading(true);
        setError("");
        const orgsResponse = await listOrganizationsApiV1SuperuserOrganizationsGet();
        if (orgsResponse.error) {
            setError(detailFromError(orgsResponse.error, "Failed to load organizations"));
        } else if (orgsResponse.data) {
            setOrganizations(orgsResponse.data.organizations);
        }
        setIsLoading(false);
    }, []);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        fetchOrganizations();
    }, [authLoading, user, fetchOrganizations]);

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-5xl">
            <div className="flex items-center gap-2">
                <Link href="/superadmin">
                    <Button variant="ghost" size="sm">
                        <ArrowLeft className="h-4 w-4" />
                    </Button>
                </Link>
                <div>
                    <h1 className="text-3xl font-bold">Wallets</h1>
                    <p className="text-sm text-muted-foreground mt-1">
                        Configure billing rates and manage prepaid balances for client organizations.
                    </p>
                </div>
            </div>

            <Card>
                <CardHeader>
                    <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                        <div>
                            <CardTitle>
                                {filterMode === "managed" ? "Your Organizations" : "All Organizations"}
                            </CardTitle>
                            <CardDescription>
                                {filterMode === "managed"
                                    ? `${managedCount} ${managedCount === 1 ? "organization" : "organizations"} you created`
                                    : `${organizations.length} ${organizations.length === 1 ? "organization" : "organizations"} on the platform`}
                            </CardDescription>
                        </div>
                        <Tabs
                            value={filterMode}
                            onValueChange={(v) => setFilterMode(v as "managed" | "all")}
                        >
                            <TabsList>
                                <TabsTrigger value="managed">Managed</TabsTrigger>
                                <TabsTrigger value="all">All</TabsTrigger>
                            </TabsList>
                        </Tabs>
                    </div>
                </CardHeader>
                <CardContent>
                    {error && (
                        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                            {error}
                        </div>
                    )}
                    {isLoading ? (
                        <div className="flex items-center justify-center py-12 text-muted-foreground">
                            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                            Loading organizations...
                        </div>
                    ) : visibleOrganizations.length === 0 && !error ? (
                        <div className="py-12 text-center text-sm text-muted-foreground">
                            {filterMode === "managed" ? (
                                <>
                                    No managed organizations yet.
                                    {organizations.length > 0 && " Switch to All to see every organization on the platform."}
                                </>
                            ) : (
                                "No organizations yet."
                            )}
                        </div>
                    ) : (
                        <div className="overflow-x-auto">
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>Name</TableHead>
                                        <TableHead>Status</TableHead>
                                        <TableHead className="text-right">Actions</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {visibleOrganizations.map((org) => (
                                        <TableRow key={org.id}>
                                            <TableCell>
                                                <div className="flex flex-col gap-0.5">
                                                    <span className="font-medium">
                                                        {org.name || "Unnamed"}
                                                    </span>
                                                    <span className="font-mono text-xs text-muted-foreground">
                                                        {org.provider_id}
                                                    </span>
                                                </div>
                                            </TableCell>
                                            <TableCell className="text-muted-foreground capitalize">
                                                {org.status.replace(/_/g, " ")}
                                            </TableCell>
                                            <TableCell className="text-right">
                                                <Link href={`/superadmin/wallets/${org.id}`}>
                                                    <Button variant="outline" size="sm">
                                                        <Wallet className="mr-2 h-4 w-4" />
                                                        Manage Wallet
                                                    </Button>
                                                </Link>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </div>
                    )}
                </CardContent>
            </Card>
        </main>
    );
}
