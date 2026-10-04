"use client";

import { ChevronDown, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import Link from "next/link";
import { Fragment, useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { useOrganizationTimezone } from "@/lib/useOrganizationTimezone";
import { cn } from "@/lib/utils";
import { listRequestLogs, type RequestLog } from "@/lib/webhookSync";

import { ErrorBox, JsonBlock, LoadingRows, prettyBody, WhenCell } from "./shared";

const PAGE_SIZE = 50;

function codeClass(code: number): string {
    if (code < 300) return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
    if (code === 429) return "bg-amber-500/15 text-amber-700 dark:text-amber-300";
    if (code < 500) return "bg-red-500/15 text-red-700 dark:text-red-300";
    return "bg-red-600/20 text-red-800 dark:text-red-200";
}

export function RequestLogsPanel({ endpointId, phoneMasked }: { endpointId: number; phoneMasked?: boolean }) {
    const { isAuthenticated, loading: authLoading } = useAuth();
    const timezone = useOrganizationTimezone();
    const [logs, setLogs] = useState<RequestLog[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [open, setOpen] = useState<number | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const result = await listRequestLogs(endpointId, PAGE_SIZE, (page - 1) * PAGE_SIZE);
            setLogs(result.logs);
            setTotal(result.total);
            setError(null);
        } catch (e) {
            setError(e instanceof Error ? e.message : "Failed to load request logs");
        } finally {
            setLoading(false);
        }
    }, [endpointId, page]);

    useEffect(() => {
        if (authLoading || !isAuthenticated) return;
        load();
    }, [authLoading, isAuthenticated, load]);

    return (
        <Card>
            <CardHeader>
                <div className="flex items-center justify-between">
                    <div>
                        <CardTitle>Request logs</CardTitle>
                        <CardDescription>
                            Every request your CRM sent, newest first, kept for 30 days. Secrets are never shown.
                            Click a row for the headers and body.
                        </CardDescription>
                    </div>
                    <Button variant="outline" size="icon" onClick={load} disabled={loading} title="Reload">
                        <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                    </Button>
                </div>
            </CardHeader>
            <CardContent>
                {error ? (
                    <ErrorBox message={error} />
                ) : loading && logs.length === 0 ? (
                    <LoadingRows />
                ) : logs.length === 0 ? (
                    <p className="py-8 text-center text-muted-foreground">
                        No requests yet. Use the test command on the Setup tab, or create a lead in your CRM.
                    </p>
                ) : (
                    <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-sm">
                        <Table>
                            <TableHeader>
                                <TableRow className="bg-muted/50">
                                    <TableHead className="w-8" />
                                    <TableHead className="font-semibold">Received</TableHead>
                                    <TableHead className="font-semibold">Result</TableHead>
                                    <TableHead className="font-semibold">Details</TableHead>
                                    <TableHead className="font-semibold">Leads</TableHead>
                                    <TableHead className="font-semibold">Time</TableHead>
                                    <TableHead className="font-semibold">From</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {logs.map((log) => (
                                    <Fragment key={log.id}>
                                        <TableRow
                                            className="cursor-pointer hover:bg-muted/50"
                                            onClick={() => setOpen(open === log.id ? null : log.id)}
                                        >
                                            <TableCell>
                                                <ChevronDown
                                                    className={cn(
                                                        "h-4 w-4 text-muted-foreground transition-transform",
                                                        open === log.id ? "" : "-rotate-90",
                                                    )}
                                                />
                                            </TableCell>
                                            <TableCell>
                                                <WhenCell iso={log.received_at} timezone={timezone} />
                                            </TableCell>
                                            <TableCell>
                                                <Badge
                                                    variant="outline"
                                                    className={cn("border-transparent font-mono", codeClass(log.response_code))}
                                                >
                                                    {log.response_code}
                                                </Badge>
                                            </TableCell>
                                            <TableCell className="max-w-md truncate text-sm" title={log.error ?? ""}>
                                                {log.error || (log.response_code < 300 ? "Stored" : "—")}
                                            </TableCell>
                                            <TableCell className="text-sm">
                                                {log.lead_ids && log.lead_ids.length > 0 ? (
                                                    <span className="flex flex-wrap gap-1">
                                                        {log.lead_ids.slice(0, 3).map((id) => (
                                                            <Link
                                                                key={id}
                                                                href={`/webhook-sync/leads/${id}`}
                                                                className="font-mono text-primary hover:underline"
                                                                onClick={(e) => e.stopPropagation()}
                                                            >
                                                                #{id}
                                                            </Link>
                                                        ))}
                                                        {log.lead_ids.length > 3 && (
                                                            <span className="text-muted-foreground">
                                                                +{log.lead_ids.length - 3}
                                                            </span>
                                                        )}
                                                    </span>
                                                ) : (
                                                    "—"
                                                )}
                                            </TableCell>
                                            <TableCell className="text-sm tabular-nums">
                                                {log.duration_ms != null ? `${log.duration_ms} ms` : "—"}
                                            </TableCell>
                                            <TableCell className="font-mono text-xs">{log.ip || "—"}</TableCell>
                                        </TableRow>
                                        {open === log.id && (
                                            <TableRow className="hover:bg-transparent">
                                                <TableCell colSpan={7} className="bg-muted/20">
                                                    <div className="grid gap-4 py-2 md:grid-cols-2">
                                                        <div>
                                                            <p className="mb-1 text-xs font-medium text-muted-foreground">
                                                                Headers
                                                            </p>
                                                            <JsonBlock value={log.headers} maxHeight="max-h-72" />
                                                        </div>
                                                        <div>
                                                            <p className="mb-1 text-xs font-medium text-muted-foreground">
                                                                Body{log.content_type ? ` (${log.content_type})` : ""}
                                                                {log.body_truncated ? ", truncated" : ""}
                                                            </p>
                                                            {log.raw_body ? (
                                                                <JsonBlock value={prettyBody(log.raw_body)} maxHeight="max-h-72" />
                                                            ) : (
                                                                <p className="text-sm text-muted-foreground">
                                                                    {phoneMasked
                                                                        ? "Hidden because phone numbers are masked for this organization."
                                                                        : "No body."}
                                                                </p>
                                                            )}
                                                        </div>
                                                    </div>
                                                </TableCell>
                                            </TableRow>
                                        )}
                                    </Fragment>
                                ))}
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
                            <Button variant="outline" size="sm" onClick={() => setPage(page - 1)} disabled={page === 1}>
                                <ChevronLeft className="h-4 w-4" />
                                Previous
                            </Button>
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => setPage(page + 1)}
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
    );
}
