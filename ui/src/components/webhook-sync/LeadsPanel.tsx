"use client";

import { ChevronLeft, ChevronRight, Download, ExternalLink, PhoneCall, PhoneOff, RefreshCw, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { FilterBuilder } from "@/components/filters/FilterBuilder";
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAuth } from "@/lib/auth";
import { decodeFiltersFromURL, encodeFiltersToURL } from "@/lib/filters";
import { useOrganizationTimezone } from "@/lib/useOrganizationTimezone";
import {
    bulkLeadAction,
    CALLABLE_AGAIN,
    downloadLeadsCsv,
    getLeadStats,
    getSyncStats,
    leadFilterAttributes,
    leadQueryFromFilters,
    type LeadStats,
    listLeads,
    SKIPPABLE,
    summarizeBulkResult,
    type SyncStats,
    type WebhookLead,
} from "@/lib/webhookSync";
import type { ActiveFilter } from "@/types/filters";

import { ErrorBox, LeadStatsCards, LeadStatusBadge, LoadingRows, WhenCell } from "./shared";

const PAGE_SIZE = 50;

interface LeadsPanelProps {
    /** Only this endpoint's leads; all of the organization's when omitted. */
    endpointId?: number;
    /** Endpoint names by id, for the Endpoint column on the all-leads view. */
    endpointNames?: Record<number, string>;
    /** Workflow id per endpoint, to link a lead's call to its run page. */
    endpointWorkflows?: Record<number, number>;
    /** Called after leads were called or skipped from here (counts elsewhere may change). */
    onLeadsChanged?: () => void;
}

export function LeadsPanel({ endpointId, endpointNames, endpointWorkflows, onLeadsChanged }: LeadsPanelProps) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const { isAuthenticated, loading: authLoading } = useAuth();
    const timezone = useOrganizationTimezone();

    const [page, setPage] = useState(() => Math.max(1, parseInt(searchParams.get("page") || "1", 10) || 1));
    const [activeFilters, setActiveFilters] = useState<ActiveFilter[]>(() =>
        decodeFiltersFromURL(searchParams, leadFilterAttributes),
    );
    const [appliedFilters, setAppliedFilters] = useState<ActiveFilter[]>(() =>
        decodeFiltersFromURL(searchParams, leadFilterAttributes),
    );
    const [leads, setLeads] = useState<WebhookLead[]>([]);
    const [total, setTotal] = useState(0);
    const [stats, setStats] = useState<LeadStats | null>(null);
    const [sync, setSync] = useState<SyncStats | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [executing, setExecuting] = useState(false);
    const [exporting, setExporting] = useState(false);
    // Leads ticked on this page, for "Call selected" / "Don't call".
    const [selected, setSelected] = useState<Set<number>>(new Set());
    const [acting, setActing] = useState(false);
    const [confirmCall, setConfirmCall] = useState(false);

    const showEndpoint = endpointId === undefined;
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

    const fetchLeads = useCallback(
        async (pageNumber: number, filters: ActiveFilter[]) => {
            setLoading(true);
            try {
                const [list, counts] = await Promise.all([
                    listLeads({
                        endpointId,
                        ...leadQueryFromFilters(filters),
                        limit: PAGE_SIZE,
                        offset: (pageNumber - 1) * PAGE_SIZE,
                    }),
                    getLeadStats(endpointId),
                ]);
                setLeads(list.leads);
                setTotal(list.total);
                setSelected(new Set());
                setStats(counts);
                setError(null);
                // Secondary numbers; the table shouldn't wait for (or fail on) them.
                getSyncStats(endpointId).then(setSync).catch(() => setSync(null));
            } catch (e) {
                setError(e instanceof Error ? e.message : "Failed to load leads");
            } finally {
                setLoading(false);
            }
        },
        [endpointId],
    );

    useEffect(() => {
        if (authLoading || !isAuthenticated) return;
        fetchLeads(page, appliedFilters);
    }, [authLoading, isAuthenticated, fetchLeads, page, appliedFilters]);

    const updateUrl = useCallback(
        (pageNumber: number, filters: ActiveFilter[]) => {
            // Keep unrelated params (e.g. the selected tab).
            const params = new URLSearchParams(searchParams.toString());
            params.delete("filters");
            params.set("page", String(pageNumber));
            const encoded = encodeFiltersToURL(filters);
            if (encoded) new URLSearchParams(encoded).forEach((v, k) => params.set(k, v));
            router.push(`${pathname}?${params.toString()}`, { scroll: false });
        },
        [router, pathname, searchParams],
    );

    const apply = async () => {
        setExecuting(true);
        setPage(1);
        setAppliedFilters(activeFilters);
        updateUrl(1, activeFilters);
        setExecuting(false);
    };

    const clear = async () => {
        setActiveFilters([]);
        setAppliedFilters([]);
        setPage(1);
        updateUrl(1, []);
    };

    const goToPage = (next: number) => {
        setPage(next);
        updateUrl(next, appliedFilters);
    };

    const selectedLeads = leads.filter((lead) => selected.has(lead.id));
    const callable = selectedLeads.filter((lead) => CALLABLE_AGAIN.has(lead.status) && lead.phone);
    const skippable = selectedLeads.filter((lead) => SKIPPABLE.has(lead.status));
    const allOnPage = leads.length > 0 && leads.every((lead) => selected.has(lead.id));

    const toggle = (id: number, on: boolean) =>
        setSelected((current) => {
            const next = new Set(current);
            if (on) next.add(id);
            else next.delete(id);
            return next;
        });

    const act = async (action: "call" | "skip") => {
        const ids = (action === "call" ? callable : skippable).map((lead) => lead.id);
        if (ids.length === 0) return;
        setActing(true);
        try {
            const { message, ok } = summarizeBulkResult(await bulkLeadAction(ids, action));
            (ok ? toast.success : toast.warning)(message);
            onLeadsChanged?.();
            await fetchLeads(page, appliedFilters);
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "That didn't work");
        } finally {
            setActing(false);
        }
    };

    const runUrl = useMemo(
        () => (lead: WebhookLead) => {
            const workflowId = endpointWorkflows?.[lead.endpoint_id];
            return lead.last_workflow_run_id && workflowId
                ? `/workflow/${workflowId}/run/${lead.last_workflow_run_id}`
                : null;
        },
        [endpointWorkflows],
    );

    return (
        <div className="space-y-6">
            <LeadStatsCards stats={stats} sync={sync} />

            <FilterBuilder
                availableAttributes={leadFilterAttributes}
                activeFilters={activeFilters}
                onFiltersChange={setActiveFilters}
                onApplyFilters={apply}
                onClearFilters={clear}
                isExecuting={executing}
                hasAppliedFilters={appliedFilters.length > 0}
                title="Filter leads"
                description="By when they arrived, their status, or a name, phone or email"
                templates={[]}
            />

            <Card>
                <CardHeader>
                    <div className="flex items-center justify-between">
                        <div>
                            <CardTitle>Leads</CardTitle>
                            <CardDescription>
                                {loading && leads.length === 0
                                    ? "Loading…"
                                    : `Showing ${leads.length} of ${total} leads`}
                            </CardDescription>
                        </div>
                        <div className="flex gap-2">
                        <Button
                            variant="outline"
                            size="sm"
                            disabled={exporting || total === 0}
                            onClick={async () => {
                                setExporting(true);
                                try {
                                    await downloadLeadsCsv({ endpointId, ...leadQueryFromFilters(appliedFilters) });
                                } catch (e) {
                                    toast.error(e instanceof Error ? e.message : "Could not export the leads");
                                } finally {
                                    setExporting(false);
                                }
                            }}
                        >
                            <Download className="mr-2 h-4 w-4" />
                            {exporting ? "Exporting…" : "Export CSV"}
                        </Button>
                        <Button
                            variant="outline"
                            size="icon"
                            onClick={() => fetchLeads(page, appliedFilters)}
                            disabled={loading}
                            title="Reload"
                        >
                            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                        </Button>
                        </div>
                    </div>
                </CardHeader>
                <CardContent>
                    {selected.size > 0 && (
                        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border bg-muted/40 px-4 py-2 text-sm">
                            <span className="font-medium">{selected.size} selected</span>
                            <Button
                                size="sm"
                                disabled={acting || callable.length === 0}
                                onClick={() => setConfirmCall(true)}
                                title={callable.length === 0 ? "None of the selected leads can be called now" : undefined}
                            >
                                <PhoneCall className="mr-2 h-4 w-4" />
                                Call selected{callable.length !== selected.size ? ` (${callable.length})` : ""}
                            </Button>
                            <Button
                                size="sm"
                                variant="outline"
                                disabled={acting || skippable.length === 0}
                                onClick={() => act("skip")}
                                title={
                                    skippable.length === 0
                                        ? "Only leads on hold, or not yet queued, can be skipped"
                                        : "They won't be called; their numbers aren't blocked"
                                }
                            >
                                <PhoneOff className="mr-2 h-4 w-4" />
                                Don&apos;t call{skippable.length !== selected.size ? ` (${skippable.length})` : ""}
                            </Button>
                            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())} disabled={acting}>
                                <X className="mr-1 h-4 w-4" />
                                Clear
                            </Button>
                        </div>
                    )}
                    <AlertDialog open={confirmCall} onOpenChange={setConfirmCall}>
                        <AlertDialogContent>
                            <AlertDialogHeader>
                                <AlertDialogTitle>
                                    Call {callable.length} lead{callable.length === 1 ? "" : "s"}?
                                </AlertDialogTitle>
                                <AlertDialogDescription>
                                    They are queued now and called within each endpoint&apos;s calling hours. Numbers
                                    that opted out are never called.
                                    {callable.length !== selected.size &&
                                        ` ${selected.size - callable.length} of the selected leads can't be called now and are left as they are.`}
                                </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                                <AlertDialogCancel>Cancel</AlertDialogCancel>
                                <AlertDialogAction onClick={() => act("call")}>Call</AlertDialogAction>
                            </AlertDialogFooter>
                        </AlertDialogContent>
                    </AlertDialog>
                    {error ? (
                        <ErrorBox message={error} />
                    ) : loading && leads.length === 0 ? (
                        <LoadingRows />
                    ) : leads.length === 0 ? (
                        <div className="py-8 text-center text-muted-foreground">
                            {appliedFilters.length > 0
                                ? "No leads match these filters"
                                : "No leads yet. They appear here as soon as your CRM sends one."}
                        </div>
                    ) : (
                        <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-sm">
                            <Table>
                                <TableHeader>
                                    <TableRow className="bg-muted/50">
                                        <TableHead className="w-10">
                                            <Checkbox
                                                aria-label="Select all leads on this page"
                                                checked={allOnPage}
                                                onCheckedChange={(on) =>
                                                    setSelected(on ? new Set(leads.map((lead) => lead.id)) : new Set())
                                                }
                                            />
                                        </TableHead>
                                        <TableHead className="font-semibold">ID</TableHead>
                                        <TableHead className="font-semibold">Received</TableHead>
                                        <TableHead className="font-semibold">Name</TableHead>
                                        <TableHead className="font-semibold">Phone</TableHead>
                                        <TableHead className="font-semibold">Source</TableHead>
                                        <TableHead className="font-semibold">City</TableHead>
                                        <TableHead className="font-semibold">Status</TableHead>
                                        <TableHead className="font-semibold">Attempts</TableHead>
                                        <TableHead className="font-semibold">Last call</TableHead>
                                        {showEndpoint && <TableHead className="font-semibold">Endpoint</TableHead>}
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {leads.map((lead) => {
                                        const run = runUrl(lead);
                                        const city = lead.variables?.city;
                                        return (
                                            <TableRow
                                                key={lead.id}
                                                className="cursor-pointer hover:bg-muted/50"
                                                onClick={() => router.push(`/webhook-sync/leads/${lead.id}`)}
                                            >
                                                <TableCell onClick={(e) => e.stopPropagation()}>
                                                    <Checkbox
                                                        aria-label={`Select lead #${lead.id}`}
                                                        checked={selected.has(lead.id)}
                                                        onCheckedChange={(on) => toggle(lead.id, on === true)}
                                                    />
                                                </TableCell>
                                                <TableCell className="font-mono text-sm">#{lead.id}</TableCell>
                                                <TableCell>
                                                    <WhenCell iso={lead.received_at} timezone={timezone} />
                                                </TableCell>
                                                <TableCell className="text-sm">{lead.name || "—"}</TableCell>
                                                <TableCell className="whitespace-nowrap font-mono text-sm">
                                                    {lead.phone || lead.phone_raw || "—"}
                                                </TableCell>
                                                <TableCell className="text-sm">{lead.source || "—"}</TableCell>
                                                <TableCell className="text-sm">
                                                    {typeof city === "string" && city ? city : "—"}
                                                </TableCell>
                                                <TableCell>
                                                    {lead.status_reason ? (
                                                        <Tooltip>
                                                            <TooltipTrigger asChild>
                                                                <span>
                                                                    <LeadStatusBadge status={lead.status} />
                                                                </span>
                                                            </TooltipTrigger>
                                                            <TooltipContent>{lead.status_reason}</TooltipContent>
                                                        </Tooltip>
                                                    ) : (
                                                        <LeadStatusBadge status={lead.status} />
                                                    )}
                                                </TableCell>
                                                <TableCell className="text-sm tabular-nums">{lead.call_attempts}</TableCell>
                                                <TableCell className="text-sm">
                                                    {run ? (
                                                        <Button
                                                            variant="outline"
                                                            size="sm"
                                                            onClick={(e) => {
                                                                e.stopPropagation();
                                                                window.open(run, "_blank");
                                                            }}
                                                        >
                                                            #{lead.last_workflow_run_id}
                                                            <ExternalLink className="ml-1 h-3.5 w-3.5" />
                                                        </Button>
                                                    ) : (
                                                        <span className="text-muted-foreground">—</span>
                                                    )}
                                                </TableCell>
                                                {showEndpoint && (
                                                    <TableCell className="text-sm">
                                                        {endpointNames?.[lead.endpoint_id] ?? `#${lead.endpoint_id}`}
                                                    </TableCell>
                                                )}
                                            </TableRow>
                                        );
                                    })}
                                </TableBody>
                            </Table>
                        </div>
                    )}

                    {totalPages > 1 && (
                        <div className="mt-6 flex items-center justify-between">
                            <p className="text-sm text-muted-foreground">
                                Page {page} of {totalPages}
                            </p>
                            <div className="flex gap-2">
                                <Button variant="outline" size="sm" onClick={() => goToPage(page - 1)} disabled={page === 1}>
                                    <ChevronLeft className="h-4 w-4" />
                                    Previous
                                </Button>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => goToPage(page + 1)}
                                    disabled={page >= totalPages}
                                >
                                    Next
                                    <ChevronRight className="h-4 w-4" />
                                </Button>
                            </div>
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}
