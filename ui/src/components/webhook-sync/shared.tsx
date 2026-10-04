"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { formatDateTime12h } from "@/lib/dateTime";
import { cn } from "@/lib/utils";
import { formatWait, type LeadStats, leadStatusMeta, type SyncStats } from "@/lib/webhookSync";

export function LeadStatusBadge({ status }: { status: string }) {
    const meta = leadStatusMeta(status);
    return (
        <Badge variant="outline" className={cn("border-transparent font-medium", meta.className)}>
            {meta.label}
        </Badge>
    );
}

export function WhenCell({ iso, timezone }: { iso?: string | null; timezone?: string | null }) {
    return <span className="whitespace-nowrap text-sm">{formatDateTime12h(iso, timezone)}</span>;
}

function StatCard({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
    return (
        <Card>
            <CardContent className="p-4">
                <p className="text-sm text-muted-foreground">{label}</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
                {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
            </CardContent>
        </Card>
    );
}

/** Summary cards above a leads table. */
export function LeadStatsCards({ stats, sync }: { stats: LeadStats | null; sync?: SyncStats | null }) {
    const n = (status: string) => stats?.by_status[status] ?? 0;
    const dash = stats ? undefined : "—";
    const rate = sync?.connect_rate;
    const callbacks = sync ? Object.values(sync.callbacks).reduce((a, b) => a + b, 0) : 0;
    return (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
            <StatCard label="Leads today" value={dash ?? stats!.today} hint={stats ? `${stats.total} in total` : undefined} />
            <StatCard
                label="Called"
                value={sync ? sync.called : "—"}
                hint={sync ? `of ${sync.callable} callable, last ${sync.days} days` : undefined}
            />
            <StatCard
                label="Connect rate"
                value={rate == null ? "—" : `${Math.round(rate * 100)}%`}
                hint={sync ? `${sync.connected} connected` : undefined}
            />
            <StatCard
                label="Time to first call"
                value={formatWait(sync?.median_seconds_to_first_call)}
                hint="Median, from lead to call"
            />
            <StatCard
                label="Not called"
                value={dash ?? n("duplicate") + n("invalid_number")}
                hint={stats ? `${n("duplicate")} duplicates, ${n("invalid_number")} invalid` : undefined}
            />
            <StatCard
                label="Request errors"
                value={sync ? sync.request_errors : "—"}
                hint={
                    sync
                        ? `of ${sync.requests} requests` +
                          (callbacks ? `; ${sync.callbacks.dead_letter ?? 0} failed CRM callbacks` : "")
                        : undefined
                }
            />
        </div>
    );
}

export function JsonBlock({ value, maxHeight = "max-h-96" }: { value: unknown; maxHeight?: string }) {
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    return (
        <pre className={cn("overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed", maxHeight)}>{text}</pre>
    );
}

/** A body as pretty JSON when it parses, otherwise as sent. */
export function prettyBody(body?: string | null): string {
    if (!body) return "";
    try {
        return JSON.stringify(JSON.parse(body), null, 2);
    } catch {
        return body;
    }
}

export function ErrorBox({ message }: { message: string }) {
    return (
        <div className="rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {message}
        </div>
    );
}

export function LoadingRows({ rows = 5 }: { rows?: number }) {
    return (
        <div className="animate-pulse space-y-3">
            {Array.from({ length: rows }, (_, i) => (
                <div key={i} className="h-12 rounded bg-muted" />
            ))}
        </div>
    );
}
