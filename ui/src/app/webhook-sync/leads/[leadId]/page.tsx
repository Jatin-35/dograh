"use client";

import { ArrowLeft, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { LeadActions } from "@/components/webhook-sync/LeadActions";
import { ErrorBox, JsonBlock, LeadStatusBadge } from "@/components/webhook-sync/shared";
import { useAuth } from "@/lib/auth";
import { formatDateTime12h } from "@/lib/dateTime";
import { useOrganizationTimezone } from "@/lib/useOrganizationTimezone";
import {
    CALLBACK_STATUS_LABELS,
    getEndpoint,
    getLead,
    leadTimeline,
    type WebhookEndpoint,
    type WebhookLead,
} from "@/lib/webhookSync";

function Detail({ label, value, mono }: { label: string; value?: string | number | null; mono?: boolean }) {
    return (
        <div>
            <p className="text-xs text-muted-foreground">{label}</p>
            <p className={`mt-0.5 break-all text-sm ${mono ? "font-mono" : ""}`}>{value ?? "—"}</p>
        </div>
    );
}

export default function LeadDetailPage() {
    const router = useRouter();
    const params = useParams();
    const leadId = Number(params.leadId);
    const { user, loading: authLoading, redirectToLogin } = useAuth();
    // Whether someone is signed in, not the user object: a new object for the
    // same user (e.g. after a token refresh) must not re-run the fetches.
    const signedIn = Boolean(user);
    const timezone = useOrganizationTimezone();
    const [lead, setLead] = useState<WebhookLead | null>(null);
    const [endpoint, setEndpoint] = useState<WebhookEndpoint | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!authLoading && !signedIn) redirectToLogin();
    }, [authLoading, signedIn, redirectToLogin]);

    const load = useCallback(async () => {
        try {
            const found = await getLead(leadId);
            setLead(found);
            // The endpoint only adds its name and the agent for run links.
            getEndpoint(found.endpoint_id).then(setEndpoint).catch(() => undefined);
        } catch (e) {
            setError(e instanceof Error ? e.message : "Failed to load the lead");
        }
    }, [leadId]);

    useEffect(() => {
        if (authLoading || !signedIn || !Number.isFinite(leadId)) return;
        load();
    }, [authLoading, signedIn, leadId, load]);

    const back = () => {
        if (window.history.length > 1) router.back();
        else router.push("/webhook-sync?tab=leads");
    };

    if (error) {
        return (
            <div className="mx-auto max-w-4xl space-y-4 p-6">
                <Button variant="ghost" onClick={back}>
                    <ArrowLeft className="mr-2 h-4 w-4" />
                    Back
                </Button>
                <ErrorBox message={error} />
            </div>
        );
    }

    if (!lead) {
        return (
            <div className="mx-auto max-w-4xl p-6">
                <div className="animate-pulse space-y-4">
                    <div className="h-8 w-1/3 rounded bg-muted" />
                    <div className="h-48 rounded bg-muted" />
                    <div className="h-48 rounded bg-muted" />
                </div>
            </div>
        );
    }

    const when = (iso?: string | null) => formatDateTime12h(iso, timezone);
    const variables = Object.entries(lead.variables ?? {});
    const runUrl =
        lead.last_workflow_run_id && endpoint
            ? `/workflow/${endpoint.workflow_id}/run/${lead.last_workflow_run_id}`
            : null;
    const timeline = leadTimeline(lead);

    return (
        <div className="mx-auto max-w-4xl space-y-6 p-6">
            <Button variant="ghost" onClick={back}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back
            </Button>

            <Card>
                <CardHeader>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                            <CardTitle className="text-2xl">{lead.name || lead.phone || `Lead #${lead.id}`}</CardTitle>
                            <CardDescription className="mt-1 flex items-center gap-1">
                                Lead <span className="font-mono">#{lead.id}</span>
                                <CopyButton value={String(lead.id)} label="Lead ID" /> · received {when(lead.received_at)}
                                {endpoint && (
                                    <>
                                        {" "}
                                        via{" "}
                                        <Link href={`/webhook-sync/${endpoint.id}`} className="text-primary hover:underline">
                                            {endpoint.name}
                                        </Link>
                                    </>
                                )}
                            </CardDescription>
                        </div>
                        <div className="flex flex-col items-end gap-2">
                            <LeadStatusBadge status={lead.status} />
                            <LeadActions lead={lead} onChanged={load} />
                        </div>
                    </div>
                </CardHeader>
                <CardContent className="space-y-4">
                    {lead.status_reason && (
                        <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
                            {lead.status_reason}
                            {lead.duplicate_of_lead_id && (
                                <>
                                    {" "}
                                    (first received as{" "}
                                    <Link
                                        href={`/webhook-sync/leads/${lead.duplicate_of_lead_id}`}
                                        className="font-mono text-primary hover:underline"
                                    >
                                        lead #{lead.duplicate_of_lead_id}
                                    </Link>
                                    )
                                </>
                            )}
                        </div>
                    )}
                    <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
                        <Detail label="Phone" value={lead.phone} mono />
                        <Detail label="Phone as sent" value={lead.phone_raw} mono />
                        <Detail label="Email" value={lead.email} />
                        <Detail label="Source" value={lead.source} />
                        <Detail label="City" value={typeof lead.variables?.city === "string" ? lead.variables.city : null} />
                        <Detail label="CRM lead ID" value={lead.external_lead_id} mono />
                    </div>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Timeline</CardTitle>
                </CardHeader>
                <CardContent>
                    <ol className="relative space-y-4 border-l pl-6">
                        {timeline.map((step, index) => (
                            <li key={index}>
                                <span
                                    className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full border-2 border-background ${
                                        step.tone === "bad"
                                            ? "bg-destructive"
                                            : step.tone === "good"
                                              ? "bg-emerald-500"
                                              : step.tone === "pending"
                                                ? "bg-muted-foreground/40"
                                                : "bg-primary"
                                    }`}
                                />
                                <p className="text-sm font-medium">{step.title}</p>
                                {step.detail && <p className="text-sm text-muted-foreground">{step.detail}</p>}
                                {step.at && <p className="text-xs text-muted-foreground">{when(step.at)}</p>}
                            </li>
                        ))}
                    </ol>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Calls</CardTitle>
                    <CardDescription>
                        {lead.call_attempts === 0
                            ? "Not called yet."
                            : `${lead.call_attempts} attempt${lead.call_attempts === 1 ? "" : "s"}.`}
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    {lead.callback && (
                        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm">
                            <span className="font-medium">Result sent to CRM:</span>
                            <span
                                className={
                                    lead.callback.status === "succeeded"
                                        ? "text-emerald-700 dark:text-emerald-300"
                                        : lead.callback.status === "dead_letter"
                                          ? "text-destructive"
                                          : "text-muted-foreground"
                                }
                            >
                                {CALLBACK_STATUS_LABELS[lead.callback.status] ?? lead.callback.status}
                            </span>
                            <span className="text-muted-foreground">
                                · {lead.callback.attempts} attempt{lead.callback.attempts === 1 ? "" : "s"}
                                {lead.callback.last_status_code ? ` · HTTP ${lead.callback.last_status_code}` : ""}
                            </span>
                            {lead.callback.last_error && lead.callback.status !== "succeeded" && (
                                <span className="w-full truncate text-xs text-muted-foreground" title={lead.callback.last_error}>
                                    {lead.callback.last_error}
                                </span>
                            )}
                        </div>
                    )}
                    {runUrl ? (
                        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3">
                            <div className="text-sm">
                                <p className="font-medium">
                                    Last call <span className="font-mono">#{lead.last_workflow_run_id}</span>
                                </p>
                                <p className="text-muted-foreground">
                                    {[lead.last_call_status, lead.disposition].filter(Boolean).join(" · ") || "—"}
                                </p>
                            </div>
                            <Button variant="outline" size="sm" onClick={() => window.open(runUrl, "_blank")}>
                                Transcript & recording
                                <ExternalLink className="ml-2 h-4 w-4" />
                            </Button>
                        </div>
                    ) : (
                        <p className="text-sm text-muted-foreground">
                            Each call to this lead will be listed here, linked to its transcript and recording.
                        </p>
                    )}
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Call variables</CardTitle>
                    <CardDescription>What the agent can use in its prompt, e.g. {"{{name}}"}.</CardDescription>
                </CardHeader>
                <CardContent>
                    {variables.length === 0 ? (
                        <p className="text-sm text-muted-foreground">None.</p>
                    ) : (
                        <div className="divide-y rounded-md border">
                            {variables.map(([key, value]) => (
                                <div key={key} className="grid grid-cols-[minmax(8rem,14rem)_1fr] gap-3 px-3 py-2 text-sm">
                                    <span className="font-mono text-muted-foreground">{`{{${key}}}`}</span>
                                    <span className="break-all">{String(value)}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <div className="flex items-center justify-between">
                        <div>
                            <CardTitle>Original payload</CardTitle>
                            <CardDescription>Exactly what your CRM sent for this lead. Kept for 30 days.</CardDescription>
                        </div>
                        {lead.raw_payload != null && (
                            <CopyButton value={JSON.stringify(lead.raw_payload, null, 2)} label="Payload" />
                        )}
                    </div>
                </CardHeader>
                <CardContent>
                    {lead.raw_payload != null ? (
                        <JsonBlock value={lead.raw_payload} />
                    ) : (
                        <p className="text-sm text-muted-foreground">
                            {lead.phone_masked
                                ? "Hidden because phone numbers are masked for this organization."
                                : "No longer kept (payloads are cleared after 30 days)."}
                        </p>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}
