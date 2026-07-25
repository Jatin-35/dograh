"use client";

import { AlertTriangle, ArrowLeft, Building2, Loader2, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { CopyButton } from "@/components/CopyButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/lib/auth";
import {
    createSuperadminOrganization,
    listSuperadminOrganizations,
    type OrganizationStatus,
    type SuperadminOrganization,
    updateSuperadminOrganizationStatus,
} from "@/lib/superadminOrganizations";

const STATUS_BADGE: Record<
    OrganizationStatus,
    { label: string; variant: "default" | "secondary" | "destructive" | "success" }
> = {
    active: { label: "Active", variant: "success" },
    pending_setup: { label: "Pending Setup", variant: "secondary" },
    suspended: { label: "Suspended", variant: "destructive" },
};

function formatDate(dateString: string): string {
    return new Date(dateString).toLocaleString();
}

export default function OrganizationsPage() {
    const { user, loading: authLoading } = useAuth();
    const [organizations, setOrganizations] = useState<SuperadminOrganization[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    // Create modal state
    const [createOpen, setCreateOpen] = useState(false);
    const [newName, setNewName] = useState("");
    const [newEmail, setNewEmail] = useState("");
    const [isCreating, setIsCreating] = useState(false);
    const [createError, setCreateError] = useState("");

    // Per-row status-change in-flight guard
    const [statusUpdatingId, setStatusUpdatingId] = useState<number | null>(null);

    // "managed" = orgs you provisioned via the panel (they carry a contact
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
        try {
            const orgs = await listSuperadminOrganizations();
            setOrganizations(orgs);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to load organizations");
        } finally {
            setIsLoading(false);
        }
    }, []);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        fetchOrganizations();
    }, [authLoading, user, fetchOrganizations]);

    const handleCreate = async () => {
        const name = newName.trim();
        const email = newEmail.trim();
        setCreateError("");
        if (!name || !email) {
            setCreateError("Both name and email are required.");
            return;
        }
        setIsCreating(true);
        try {
            const result = await createSuperadminOrganization(name, email);
            if (result.invitation_sent) {
                toast.success(`Organization created and invite sent to ${email}.`);
            } else {
                toast.warning(
                    `Organization created, but the invite email to ${email} could not be sent.`,
                );
            }
            setCreateOpen(false);
            setNewName("");
            setNewEmail("");
            await fetchOrganizations();
        } catch (err) {
            setCreateError(err instanceof Error ? err.message : "Failed to create organization");
        } finally {
            setIsCreating(false);
        }
    };

    const handleStatusChange = async (
        org: SuperadminOrganization,
        nextStatus: OrganizationStatus,
    ) => {
        setStatusUpdatingId(org.id);
        try {
            const updated = await updateSuperadminOrganizationStatus(org.id, nextStatus);
            setOrganizations((prev) =>
                prev.map((o) => (o.id === org.id ? { ...o, status: updated.status } : o)),
            );
            toast.success(
                nextStatus === "suspended"
                    ? "Organization suspended."
                    : "Organization activated.",
            );
        } catch (err) {
            toast.error(err instanceof Error ? err.message : "Failed to update status");
        } finally {
            setStatusUpdatingId(null);
        }
    };

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-6xl">
            <div className="flex items-center justify-between">
                <div>
                    <div className="flex items-center gap-2">
                        <Link href="/superadmin">
                            <Button variant="ghost" size="sm">
                                <ArrowLeft className="h-4 w-4" />
                            </Button>
                        </Link>
                        <h1 className="text-3xl font-bold">Organizations</h1>
                    </div>
                    <p className="text-sm text-muted-foreground mt-1">
                        View and manage all client organizations on the platform.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={fetchOrganizations}
                        disabled={isLoading}
                    >
                        <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
                    </Button>
                    <Button size="sm" onClick={() => setCreateOpen(true)}>
                        <Plus className="mr-2 h-4 w-4" />
                        Create Organization
                    </Button>
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
                        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                            <AlertTriangle className="h-4 w-4 shrink-0" />
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
                                    You haven&apos;t created any organizations yet. Click
                                    &ldquo;Create Organization&rdquo; to invite your first client
                                    {organizations.length > 0
                                        ? ", or switch to All to see every organization on the platform."
                                        : "."}
                                </>
                            ) : (
                                "No organizations yet. Create one to invite your first client."
                            )}
                        </div>
                    ) : (
                        <div className="overflow-x-auto">
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>Name</TableHead>
                                        <TableHead>Status</TableHead>
                                        <TableHead>Created At</TableHead>
                                        <TableHead className="text-center">Users</TableHead>
                                        <TableHead>Email</TableHead>
                                        <TableHead className="text-right">Actions</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {visibleOrganizations.map((org) => {
                                        const badge = STATUS_BADGE[org.status] ?? {
                                            label: org.status,
                                            variant: "secondary" as const,
                                        };
                                        const isUpdating = statusUpdatingId === org.id;
                                        return (
                                            <TableRow key={org.id}>
                                                <TableCell>
                                                    <div className="flex flex-col gap-0.5">
                                                        <span
                                                            className={
                                                                org.name
                                                                    ? "font-medium"
                                                                    : "font-medium text-muted-foreground italic"
                                                            }
                                                        >
                                                            {org.name || "Unnamed"}
                                                        </span>
                                                        <div className="flex items-center gap-1">
                                                            <span className="font-mono text-xs text-muted-foreground break-all">
                                                                {org.provider_id}
                                                            </span>
                                                            <CopyButton
                                                                value={org.provider_id}
                                                                label="Organization ID"
                                                            />
                                                        </div>
                                                    </div>
                                                </TableCell>
                                                <TableCell>
                                                    <Badge variant={badge.variant}>{badge.label}</Badge>
                                                </TableCell>
                                                <TableCell className="whitespace-nowrap text-muted-foreground">
                                                    {formatDate(org.created_at)}
                                                </TableCell>
                                                <TableCell className="text-center">
                                                    {org.user_count}
                                                </TableCell>
                                                <TableCell className="text-muted-foreground">
                                                    {org.primary_contact_email ? (
                                                        <div className="flex items-center gap-1">
                                                            <span className="break-all">
                                                                {org.primary_contact_email}
                                                            </span>
                                                            <CopyButton
                                                                value={org.primary_contact_email}
                                                                label="Email"
                                                            />
                                                        </div>
                                                    ) : (
                                                        <span className="italic">None</span>
                                                    )}
                                                </TableCell>
                                                <TableCell className="text-right">
                                                    {org.status === "suspended" ? (
                                                        <Button
                                                            variant="outline"
                                                            size="sm"
                                                            disabled={isUpdating}
                                                            onClick={() => handleStatusChange(org, "active")}
                                                        >
                                                            {isUpdating ? (
                                                                <Loader2 className="h-4 w-4 animate-spin" />
                                                            ) : (
                                                                "Activate"
                                                            )}
                                                        </Button>
                                                    ) : (
                                                        <Button
                                                            variant="destructive"
                                                            size="sm"
                                                            disabled={isUpdating}
                                                            onClick={() => handleStatusChange(org, "suspended")}
                                                        >
                                                            {isUpdating ? (
                                                                <Loader2 className="h-4 w-4 animate-spin" />
                                                            ) : (
                                                                "Suspend"
                                                            )}
                                                        </Button>
                                                    )}
                                                </TableCell>
                                            </TableRow>
                                        );
                                    })}
                                </TableBody>
                            </Table>
                        </div>
                    )}
                </CardContent>
            </Card>

            <Dialog open={createOpen} onOpenChange={(open) => !isCreating && setCreateOpen(open)}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            <Building2 className="h-5 w-5" />
                            Create Organization
                        </DialogTitle>
                        <DialogDescription>
                            Creates a client organization and emails the client an invite to join it.
                            You&apos;re added as a member so you can build their workflows right away.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4 py-2">
                        <div className="space-y-2">
                            <Label htmlFor="org-name">Organization Name</Label>
                            <Input
                                id="org-name"
                                placeholder="e.g., Vectus Polymers Pvt. Ltd."
                                value={newName}
                                onChange={(e) => setNewName(e.target.value)}
                                disabled={isCreating}
                            />
                        </div>
                        <div className="space-y-2">
                            <Label htmlFor="org-email">Client Email</Label>
                            <Input
                                id="org-email"
                                type="email"
                                placeholder="client@company.com"
                                value={newEmail}
                                onChange={(e) => setNewEmail(e.target.value)}
                                disabled={isCreating}
                            />
                        </div>
                        {createError && (
                            <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                                <AlertTriangle className="h-4 w-4 shrink-0" />
                                {createError}
                            </div>
                        )}
                    </div>
                    <DialogFooter>
                        <Button
                            variant="outline"
                            onClick={() => setCreateOpen(false)}
                            disabled={isCreating}
                        >
                            Cancel
                        </Button>
                        <Button onClick={handleCreate} disabled={isCreating}>
                            {isCreating ? (
                                <>
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    Creating...
                                </>
                            ) : (
                                "Create & Invite"
                            )}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </main>
    );
}
