"use client";

import { AlertTriangle, ArrowLeft, ChevronDown, ChevronRight, ExternalLink, Folder, FolderOpen, Inbox, Loader2, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
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
import { listSuperadminWorkflows, type SuperadminWorkflow } from "@/lib/superadminWorkflows";
import { impersonateAsSuperadmin } from "@/lib/utils";

const STATUS_BADGE: Record<
    SuperadminWorkflow["organization_status"],
    { label: string; variant: "default" | "secondary" | "destructive" | "success" }
> = {
    active: { label: "Active", variant: "success" },
    pending_setup: { label: "Pending Setup", variant: "secondary" },
    suspended: { label: "Suspended", variant: "destructive" },
};

interface FolderGroup {
    folder_id: number | null; // null = Uncategorized
    folder_name: string | null;
    workflows: SuperadminWorkflow[];
}

interface OrgGroup {
    organization_id: number;
    organization_name: string | null;
    organization_provider_id: string;
    organization_status: SuperadminWorkflow["organization_status"];
    organization_primary_contact_email: string | null;
    totalAgents: number;
    hasRealFolders: boolean;
    folders: FolderGroup[]; // real folders (by name) first, Uncategorized last
}

function buildOrgGroups(workflows: SuperadminWorkflow[]): OrgGroup[] {
    const orgs = new Map<
        number,
        {
            meta: Omit<OrgGroup, "totalAgents" | "hasRealFolders" | "folders">;
            folders: Map<number | null, FolderGroup>;
        }
    >();

    for (const wf of workflows) {
        let org = orgs.get(wf.organization_id);
        if (!org) {
            org = {
                meta: {
                    organization_id: wf.organization_id,
                    organization_name: wf.organization_name,
                    organization_provider_id: wf.organization_provider_id,
                    organization_status: wf.organization_status,
                    organization_primary_contact_email: wf.organization_primary_contact_email,
                },
                folders: new Map(),
            };
            orgs.set(wf.organization_id, org);
        }
        let folder = org.folders.get(wf.folder_id);
        if (!folder) {
            folder = { folder_id: wf.folder_id, folder_name: wf.folder_name, workflows: [] };
            org.folders.set(wf.folder_id, folder);
        }
        folder.workflows.push(wf);
    }

    return Array.from(orgs.values()).map(({ meta, folders }) => {
        const realFolders = Array.from(folders.values())
            .filter((f) => f.folder_id !== null)
            .sort((a, b) => (a.folder_name ?? "").localeCompare(b.folder_name ?? ""));
        const uncategorized = folders.get(null);
        // Real folders first, Uncategorized last (matches the regular page).
        const ordered = uncategorized ? [...realFolders, uncategorized] : realFolders;
        const totalAgents = ordered.reduce((sum, f) => sum + f.workflows.length, 0);
        return {
            ...meta,
            totalAgents,
            hasRealFolders: realFolders.length > 0,
            folders: ordered,
        };
    });
}

function formatDate(dateString: string): string {
    return new Date(dateString).toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
    });
}

export default function SuperadminAgentsPage() {
    const { user, loading: authLoading, getAccessToken } = useAuth();
    const [workflows, setWorkflows] = useState<SuperadminWorkflow[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState("");
    const [openOrgs, setOpenOrgs] = useState<Set<number>>(new Set());
    const [jumpingWorkflowId, setJumpingWorkflowId] = useState<number | null>(null);
    // "managed" = orgs you provisioned via the panel (they carry a contact
    // email, and their agents are the clickable ones); "all" = every org that
    // has agents, incl. auto-created / legacy orgs.
    const [filterMode, setFilterMode] = useState<"managed" | "all">("managed");
    const hasFetched = useRef(false);

    const fetchWorkflows = useCallback(async () => {
        setIsLoading(true);
        setError("");
        try {
            const wfs = await listSuperadminWorkflows();
            setWorkflows(wfs);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to load agents");
        } finally {
            setIsLoading(false);
        }
    }, []);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        fetchWorkflows();
    }, [authLoading, user, fetchWorkflows]);

    const allGroups = useMemo(() => buildOrgGroups(workflows), [workflows]);
    const groups = useMemo(
        () =>
            filterMode === "managed"
                ? allGroups.filter((g) => !!g.organization_primary_contact_email)
                : allGroups,
        [allGroups, filterMode],
    );

    const toggleOrg = (organizationId: number) => {
        setOpenOrgs((prev) => {
            const next = new Set(prev);
            if (next.has(organizationId)) next.delete(organizationId);
            else next.add(organizationId);
            return next;
        });
    };

    const handleOpenAgent = async (group: OrgGroup, workflow: SuperadminWorkflow) => {
        if (!group.organization_primary_contact_email) return;
        setJumpingWorkflowId(workflow.id);
        try {
            const accessToken = await getAccessToken();
            await impersonateAsSuperadmin({
                accessToken,
                email: group.organization_primary_contact_email,
                targetOrganizationId: group.organization_id,
                redirectPath: `/workflow/${workflow.id}`,
                openInNewTab: true,
            });
        } catch (err) {
            setError(err instanceof Error ? err.message : "Failed to open agent");
        } finally {
            setJumpingWorkflowId(null);
        }
    };

    const renderTable = (group: OrgGroup, folder: FolderGroup) => {
        const canOpen = !!group.organization_primary_contact_email;
        return (
            <Card className="overflow-hidden">
                <CardContent className="p-0">
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead className="font-semibold">ID</TableHead>
                                <TableHead className="font-semibold">Agent Name</TableHead>
                                <TableHead className="font-semibold">Created At</TableHead>
                                <TableHead className="font-semibold text-center">Total Runs</TableHead>
                                <TableHead className="font-semibold text-right">Actions</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {folder.workflows.map((workflow) => {
                                const isJumping = jumpingWorkflowId === workflow.id;
                                return (
                                    <TableRow key={workflow.id} className="hover:bg-accent transition-colors">
                                        <TableCell className="text-muted-foreground">{workflow.id}</TableCell>
                                        <TableCell className="font-medium">{workflow.name}</TableCell>
                                        <TableCell>{formatDate(workflow.created_at)}</TableCell>
                                        <TableCell className="text-center">
                                            <span className="inline-flex items-center justify-center min-w-[2rem] px-2 py-1 text-sm font-semibold bg-muted rounded-full">
                                                {workflow.total_runs || 0}
                                            </span>
                                        </TableCell>
                                        <TableCell className="text-right">
                                            <div className="flex justify-end">
                                                <Button
                                                    variant="outline"
                                                    size="sm"
                                                    disabled={!canOpen || isJumping}
                                                    onClick={() => handleOpenAgent(group, workflow)}
                                                    className="flex items-center gap-2"
                                                >
                                                    {isJumping ? (
                                                        <Loader2 className="h-4 w-4 animate-spin" />
                                                    ) : (
                                                        <ExternalLink size={16} />
                                                    )}
                                                    Open
                                                </Button>
                                            </div>
                                        </TableCell>
                                    </TableRow>
                                );
                            })}
                        </TableBody>
                    </Table>
                </CardContent>
            </Card>
        );
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
                        <h1 className="text-3xl font-bold">Agents</h1>
                    </div>
                    <p className="text-sm text-muted-foreground mt-1">
                        {filterMode === "managed"
                            ? "Voice agents in the client organizations you created, grouped by client."
                            : "Every voice agent across every organization, grouped by client."}
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <Tabs value={filterMode} onValueChange={(v) => setFilterMode(v as "managed" | "all")}>
                        <TabsList>
                            <TabsTrigger value="managed">Managed</TabsTrigger>
                            <TabsTrigger value="all">All</TabsTrigger>
                        </TabsList>
                    </Tabs>
                    <Button variant="outline" size="sm" onClick={fetchWorkflows} disabled={isLoading}>
                        <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
                    </Button>
                </div>
            </div>

            {error && (
                <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                    <AlertTriangle className="h-4 w-4 shrink-0" />
                    {error}
                </div>
            )}

            {isLoading ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground">
                    <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                    Loading agents...
                </div>
            ) : groups.length === 0 && !error ? (
                <Card>
                    <CardContent className="py-12 text-center text-sm text-muted-foreground">
                        {filterMode === "managed" ? (
                            <>
                                No agents in the organizations you created yet
                                {allGroups.length > 0
                                    ? " — switch to All to see agents across every organization."
                                    : "."}
                            </>
                        ) : (
                            "No agents found across any organization."
                        )}
                    </CardContent>
                </Card>
            ) : (
                <div className="space-y-3">
                    {groups.map((group) => {
                        const isOpen = openOrgs.has(group.organization_id);
                        const badge = STATUS_BADGE[group.organization_status] ?? {
                            label: group.organization_status,
                            variant: "secondary" as const,
                        };
                        const canOpen = !!group.organization_primary_contact_email;

                        return (
                            <Card key={group.organization_id}>
                                <Collapsible open={isOpen} onOpenChange={() => toggleOrg(group.organization_id)}>
                                    <CollapsibleTrigger asChild>
                                        <CardHeader className="cursor-pointer select-none hover:bg-accent/40 transition-colors">
                                            <div className="flex items-center justify-between">
                                                <div className="flex items-center gap-2">
                                                    {isOpen ? (
                                                        <ChevronDown className="h-4 w-4 text-muted-foreground shrink-0" />
                                                    ) : (
                                                        <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
                                                    )}
                                                    {isOpen ? (
                                                        <FolderOpen className="h-5 w-5 text-cta shrink-0" />
                                                    ) : (
                                                        <Folder className="h-5 w-5 text-cta shrink-0" />
                                                    )}
                                                    <div>
                                                        <CardTitle className="text-base">
                                                            {group.organization_name || (
                                                                <span className="italic text-muted-foreground">
                                                                    {group.organization_provider_id}
                                                                </span>
                                                            )}
                                                        </CardTitle>
                                                        <CardDescription>
                                                            {group.totalAgents}{" "}
                                                            {group.totalAgents === 1 ? "agent" : "agents"}
                                                        </CardDescription>
                                                    </div>
                                                </div>
                                                <Badge variant={badge.variant}>{badge.label}</Badge>
                                            </div>
                                        </CardHeader>
                                    </CollapsibleTrigger>
                                    <CollapsibleContent>
                                        <CardContent className="pt-0 space-y-4">
                                            {!canOpen && (
                                                <p className="text-xs text-muted-foreground italic">
                                                    This client hasn&apos;t accepted their invite yet — use the
                                                    team switcher to build agents for this org in the meantime.
                                                </p>
                                            )}
                                            {group.folders.map((folder) => (
                                                <div key={folder.folder_id ?? "uncategorized"} className="space-y-2">
                                                    {/* Only label folder sections when the org actually has
                                                        real folders; a single Uncategorized bucket renders as
                                                        a bare table under the org. */}
                                                    {group.hasRealFolders && (
                                                        <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                                                            {folder.folder_id === null ? (
                                                                <Inbox className="h-4 w-4" />
                                                            ) : (
                                                                <Folder className="h-4 w-4 text-cta" />
                                                            )}
                                                            <span>{folder.folder_name ?? "Uncategorized"}</span>
                                                            <span className="text-xs">({folder.workflows.length})</span>
                                                        </div>
                                                    )}
                                                    {renderTable(group, folder)}
                                                </div>
                                            ))}
                                        </CardContent>
                                    </CollapsibleContent>
                                </Collapsible>
                            </Card>
                        );
                    })}
                </div>
            )}
        </main>
    );
}
