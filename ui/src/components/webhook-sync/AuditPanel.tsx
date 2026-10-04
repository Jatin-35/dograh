"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { useOrganizationTimezone } from "@/lib/useOrganizationTimezone";
import {
    alertDetails,
    AUDIT_ACTION_LABELS,
    AUDIT_FIELD_LABELS,
    type AuditEntry,
    listAudit,
} from "@/lib/webhookSync";

import { ErrorBox, LoadingRows, WhenCell } from "./shared";

function short(value: unknown): string {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "On" : "Off";
    if (typeof value === "object") return "(changed)";
    return String(value);
}

function Changes({ entry }: { entry: AuditEntry }) {
    const alert = alertDetails(entry);
    if (alert) {
        return (
            <div className="space-y-0.5 text-sm">
                <p>{alert.message}</p>
                <p className="text-xs text-muted-foreground">
                    {alert.emailedTo.length > 0
                        ? `Emailed to ${alert.emailedTo.join(", ")}`
                        : "Not emailed (no alert email or email sending isn't set up)"}
                </p>
            </div>
        );
    }
    const changes = entry.changes as Record<string, { from: unknown; to: unknown }> | null | undefined;
    if (!changes || Object.keys(changes).length === 0) return <span className="text-muted-foreground">—</span>;
    return (
        <ul className="space-y-0.5 text-sm">
            {Object.entries(changes).map(([field, { from, to }]) => (
                <li key={field}>
                    <span className="font-medium">{AUDIT_FIELD_LABELS[field] ?? field}</span>
                    {typeof from === "object" || typeof to === "object" ? (
                        <span className="text-muted-foreground"> changed</span>
                    ) : (
                        <span className="text-muted-foreground">
                            : {short(from)} → {short(to)}
                        </span>
                    )}
                </li>
            ))}
        </ul>
    );
}

/** Who created, changed, paused, rotated or deleted endpoints. */
export function AuditPanel({ endpointId }: { endpointId?: number }) {
    const { isAuthenticated, loading: authLoading } = useAuth();
    const timezone = useOrganizationTimezone();
    const [entries, setEntries] = useState<AuditEntry[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            setEntries((await listAudit(endpointId, 100, 0)).entries);
            setError(null);
        } catch (e) {
            setError(e instanceof Error ? e.message : "Failed to load the change history");
        } finally {
            setLoading(false);
        }
    }, [endpointId]);

    useEffect(() => {
        if (authLoading || !isAuthenticated) return;
        load();
    }, [authLoading, isAuthenticated, load]);

    return (
        <Card>
            <CardHeader>
                <div className="flex items-center justify-between">
                    <div>
                        <CardTitle>History</CardTitle>
                        <CardDescription>Every change to {endpointId ? "this endpoint" : "your endpoints"}, newest first.</CardDescription>
                    </div>
                    <Button variant="outline" size="icon" onClick={load} disabled={loading} title="Reload">
                        <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                    </Button>
                </div>
            </CardHeader>
            <CardContent>
                {error ? (
                    <ErrorBox message={error} />
                ) : loading && entries.length === 0 ? (
                    <LoadingRows rows={3} />
                ) : entries.length === 0 ? (
                    <p className="py-8 text-center text-muted-foreground">No changes recorded yet.</p>
                ) : (
                    <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-sm">
                        <Table>
                            <TableHeader>
                                <TableRow className="bg-muted/50">
                                    <TableHead className="font-semibold">When</TableHead>
                                    <TableHead className="font-semibold">Action</TableHead>
                                    {!endpointId && <TableHead className="font-semibold">Endpoint</TableHead>}
                                    <TableHead className="font-semibold">What changed</TableHead>
                                    <TableHead className="font-semibold">By</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {entries.map((entry) => (
                                    <TableRow key={entry.id}>
                                        <TableCell>
                                            <WhenCell iso={entry.created_at} timezone={timezone} />
                                        </TableCell>
                                        <TableCell>
                                            <Badge
                                                variant={
                                                    entry.action === "deleted" || entry.action === "alert"
                                                        ? "destructive"
                                                        : "secondary"
                                                }
                                            >
                                                {AUDIT_ACTION_LABELS[entry.action] ?? entry.action}
                                            </Badge>
                                        </TableCell>
                                        {!endpointId && <TableCell className="text-sm">{entry.endpoint_name || "—"}</TableCell>}
                                        <TableCell>
                                            <Changes entry={entry} />
                                        </TableCell>
                                        <TableCell className="text-sm">
                                            {entry.user_email || (entry.user_id ? `User #${entry.user_id}` : "System")}
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
