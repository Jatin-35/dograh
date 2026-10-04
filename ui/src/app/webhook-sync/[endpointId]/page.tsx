"use client";

import { ArrowLeft, ListChecks, Pause, PhoneCall, PhoneOff, Play, Trash2 } from "lucide-react";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
    AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AuditPanel } from "@/components/webhook-sync/AuditPanel";
import { EndpointForm, endpointFormErrors } from "@/components/webhook-sync/EndpointForm";
import { LeadsPanel } from "@/components/webhook-sync/LeadsPanel";
import { MappingEditor } from "@/components/webhook-sync/MappingEditor";
import { RequestLogsPanel } from "@/components/webhook-sync/RequestLogsPanel";
import { SetupPanel } from "@/components/webhook-sync/SetupPanel";
import { ErrorBox } from "@/components/webhook-sync/shared";
import { useAuth } from "@/lib/auth";
import {
    callingStatus,
    cleanMapping,
    deleteEndpoint,
    type EndpointInput,
    type FieldMapping,
    getEndpoint,
    type LeadAction,
    onHoldLeadsAction,
    onHoldLeadsUrl,
    resumeCalling,
    summarizeBulkResult,
    updateEndpoint,
    type WebhookEndpoint,
} from "@/lib/webhookSync";

const TABS = ["leads", "setup", "mapping", "settings", "logs", "history"] as const;
type Tab = (typeof TABS)[number];

function toInput(endpoint: WebhookEndpoint): EndpointInput {
    return {
        name: endpoint.name,
        workflow_id: endpoint.workflow_id,
        auth_type: endpoint.auth_type,
        is_active: endpoint.is_active,
        auto_call: endpoint.auto_call,
        field_mapping: { ...endpoint.field_mapping, custom: { ...(endpoint.field_mapping.custom ?? {}) } },
        call_settings: endpoint.call_settings,
        rate_limit_per_minute: endpoint.rate_limit_per_minute,
    };
}

function SaveBar({
    dirty,
    saving,
    blocked = false,
    onSave,
    onReset,
}: {
    dirty: boolean;
    saving: boolean;
    /** The form has errors: saving is disabled, but nothing is "saving". */
    blocked?: boolean;
    onSave: () => void;
    onReset: () => void;
}) {
    return (
        <div className="flex items-center justify-end gap-2">
            {dirty && <span className="mr-auto text-sm text-muted-foreground">Unsaved changes</span>}
            <Button variant="outline" onClick={onReset} disabled={!dirty || saving}>
                Discard
            </Button>
            <Button onClick={onSave} disabled={!dirty || saving || blocked}>
                {saving ? "Saving…" : "Save changes"}
            </Button>
        </div>
    );
}

function EndpointDetail() {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();
    const params = useParams();
    const endpointId = Number(params.endpointId);
    const { user, loading: authLoading, redirectToLogin } = useAuth();
    // Whether someone is signed in, not the user object: a new object for the
    // same user (e.g. after a token refresh) must not re-run the fetches.
    const signedIn = Boolean(user);

    const [endpoint, setEndpoint] = useState<WebhookEndpoint | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [settings, setSettings] = useState<EndpointInput | null>(null);
    const [mapping, setMapping] = useState<FieldMapping | null>(null);
    const [saving, setSaving] = useState(false);
    // Asked right after resuming, when leads arrived while paused.
    const [holdPrompt, setHoldPrompt] = useState(false);

    const tabParam = searchParams.get("tab");
    const tab: Tab = TABS.includes(tabParam as Tab) ? (tabParam as Tab) : "leads";

    const adopt = useCallback((next: WebhookEndpoint) => {
        setEndpoint(next);
        setSettings(toInput(next));
        setMapping(toInput(next).field_mapping);
    }, []);

    useEffect(() => {
        if (!authLoading && !signedIn) redirectToLogin();
    }, [authLoading, signedIn, redirectToLogin]);

    useEffect(() => {
        if (authLoading || !signedIn || !Number.isFinite(endpointId)) return;
        getEndpoint(endpointId)
            .then(adopt)
            .catch((e) => setError(e instanceof Error ? e.message : "Failed to load the endpoint"));
    }, [authLoading, signedIn, endpointId, adopt]);

    const selectTab = (next: string) => router.push(`${pathname}?tab=${next}`, { scroll: false });

    // Counts only: keeps unsaved edits on the other tabs.
    const refreshEndpoint = useCallback(() => {
        getEndpoint(endpointId)
            .then(setEndpoint)
            .catch(() => undefined);
    }, [endpointId]);

    const actOnHold = async (action: LeadAction) => {
        setHoldPrompt(false);
        setSaving(true);
        try {
            const { message, ok } = summarizeBulkResult(await onHoldLeadsAction(endpointId, action));
            (ok ? toast.success : toast.warning)(message);
            refreshEndpoint();
            // Show the leads as they are now.
            router.push(`${pathname}?tab=leads`, { scroll: false });
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "That didn't work");
        } finally {
            setSaving(false);
        }
    };

    const chooseOnHold = () => {
        setHoldPrompt(false);
        router.push(onHoldLeadsUrl(endpointId), { scroll: false });
    };

    const save = async (changes: Partial<EndpointInput>, message: string) => {
        setSaving(true);
        try {
            adopt(await updateEndpoint(endpointId, changes));
            toast.success(message);
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to save");
        } finally {
            setSaving(false);
        }
    };

    const togglePaused = async () => {
        if (!endpoint) return;
        setSaving(true);
        try {
            const next = await updateEndpoint(endpointId, { is_active: !endpoint.is_active });
            // Keep unsaved edits on the other tabs.
            setEndpoint(next);
            setSettings((s) => (s ? { ...s, is_active: next.is_active } : s));
            toast.success(
                next.is_active ? "Endpoint resumed" : "Endpoint paused; new leads are kept on hold, not called",
            );
            if (next.is_active && (next.leads_on_hold ?? 0) > 0) setHoldPrompt(true);
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to save");
        } finally {
            setSaving(false);
        }
    };

    const resume = async () => {
        setSaving(true);
        try {
            setEndpoint(await resumeCalling(endpointId));
            toast.success("Calling resumed");
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to resume calling");
        } finally {
            setSaving(false);
        }
    };

    const remove = async () => {
        try {
            await deleteEndpoint(endpointId);
            toast.success("Endpoint deleted");
            router.push("/webhook-sync");
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to delete the endpoint");
        }
    };

    if (error) {
        return (
            <div className="container mx-auto space-y-4 p-6">
                <Button variant="ghost" onClick={() => router.push("/webhook-sync")}>
                    <ArrowLeft className="mr-2 h-4 w-4" />
                    Back to Webhook Sync
                </Button>
                <ErrorBox message={error} />
            </div>
        );
    }

    if (!endpoint || !settings || !mapping) {
        return (
            <div className="container mx-auto p-6">
                <div className="animate-pulse space-y-4">
                    <div className="h-8 w-1/4 rounded bg-muted" />
                    <div className="h-64 rounded bg-muted" />
                </div>
            </div>
        );
    }

    const calling = callingStatus(endpoint);
    const original = toInput(endpoint);
    const settingsDirty = JSON.stringify({ ...settings, field_mapping: null }) !== JSON.stringify({ ...original, field_mapping: null });
    const mappingDirty = JSON.stringify(cleanMapping(mapping)) !== JSON.stringify(cleanMapping(original.field_mapping));
    const settingsErrors = endpointFormErrors(settings);

    return (
        <div className="container mx-auto space-y-6 p-6">
            <Button variant="ghost" onClick={() => router.push("/webhook-sync")}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back to Webhook Sync
            </Button>

            <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <div className="mb-2 flex items-center gap-3">
                        <h1 className="text-3xl font-bold">{endpoint.name}</h1>
                        <Badge variant={endpoint.is_active ? "default" : "outline"}>
                            {endpoint.is_active ? "Active" : "Paused"}
                        </Badge>
                        <Badge
                            variant="outline"
                            className={
                                calling.tone === "good"
                                    ? "border-transparent bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                                    : calling.tone === "warn"
                                      ? "border-transparent bg-amber-500/15 text-amber-700 dark:text-amber-300"
                                      : ""
                            }
                            title={calling.hint}
                        >
                            {calling.label}
                        </Badge>
                    </div>
                    <p className="text-muted-foreground">
                        Agent: {endpoint.workflow_name ?? `#${endpoint.workflow_id}`} · {endpoint.leads_today} leads today ·{" "}
                        {endpoint.leads_total} total
                    </p>
                </div>
                <div className="flex gap-2">
                    <Button
                        variant="outline"
                        disabled={saving}
                        onClick={togglePaused}
                    >
                        {endpoint.is_active ? <Pause className="mr-2 h-4 w-4" /> : <Play className="mr-2 h-4 w-4" />}
                        {endpoint.is_active ? "Pause" : "Resume"}
                    </Button>
                    <AlertDialog>
                        <AlertDialogTrigger asChild>
                            <Button variant="outline" className="text-destructive">
                                <Trash2 className="mr-2 h-4 w-4" />
                                Delete
                            </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                            <AlertDialogHeader>
                                <AlertDialogTitle>Delete {endpoint.name}?</AlertDialogTitle>
                                <AlertDialogDescription>
                                    This deletes the endpoint with all {endpoint.leads_total} of its leads and its request
                                    logs. Your CRM&apos;s requests will be rejected. This can&apos;t be undone.
                                </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                                <AlertDialogCancel>Cancel</AlertDialogCancel>
                                <AlertDialogAction onClick={remove} className="bg-destructive text-white hover:bg-destructive/90">
                                    Delete endpoint
                                </AlertDialogAction>
                            </AlertDialogFooter>
                        </AlertDialogContent>
                    </AlertDialog>
                </div>
            </div>

            {calling.tone === "warn" && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm">
                    <span>{calling.hint}</span>
                    <Button size="sm" onClick={resume} disabled={saving}>
                        <Play className="mr-2 h-4 w-4" />
                        Resume calling
                    </Button>
                </div>
            )}

            {(endpoint.leads_on_hold ?? 0) > 0 && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-orange-500/40 bg-orange-500/10 px-4 py-3 text-sm">
                    <span>
                        <strong>{endpoint.leads_on_hold}</strong> lead{endpoint.leads_on_hold === 1 ? "" : "s"} arrived
                        while this endpoint was paused and {endpoint.leads_on_hold === 1 ? "is" : "are"} on hold, not
                        called.
                        {!endpoint.is_active && " Resume the endpoint to call them."}
                    </span>
                    <div className="flex flex-wrap gap-2">
                        <Button size="sm" onClick={() => actOnHold("call")} disabled={saving || !endpoint.is_active}>
                            <PhoneCall className="mr-2 h-4 w-4" />
                            Call all {endpoint.leads_on_hold}
                        </Button>
                        <Button size="sm" variant="outline" onClick={chooseOnHold} disabled={saving}>
                            <ListChecks className="mr-2 h-4 w-4" />
                            Choose which to call
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => actOnHold("skip")} disabled={saving}>
                            <PhoneOff className="mr-2 h-4 w-4" />
                            Don&apos;t call any
                        </Button>
                    </div>
                </div>
            )}

            <AlertDialog open={holdPrompt} onOpenChange={setHoldPrompt}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>
                            {endpoint.leads_on_hold} lead{endpoint.leads_on_hold === 1 ? "" : "s"} arrived while this
                            endpoint was paused
                        </AlertDialogTitle>
                        <AlertDialogDescription>
                            They are on hold and haven&apos;t been called. Call them all, choose which to call, or
                            don&apos;t call any (their numbers aren&apos;t blocked, so a later enquiry is still called).
                            You can also decide later from the banner on this page.
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter className="flex-wrap gap-2">
                        <AlertDialogCancel>Decide later</AlertDialogCancel>
                        <Button variant="outline" onClick={() => actOnHold("skip")}>
                            Don&apos;t call any
                        </Button>
                        <Button variant="outline" onClick={chooseOnHold}>
                            Choose which to call
                        </Button>
                        <Button onClick={() => actOnHold("call")}>Call all {endpoint.leads_on_hold}</Button>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>

            <Tabs value={tab} onValueChange={selectTab}>
                <TabsList className="flex-wrap">
                    <TabsTrigger value="leads">Leads</TabsTrigger>
                    <TabsTrigger value="setup">Setup</TabsTrigger>
                    <TabsTrigger value="mapping">Field mapping</TabsTrigger>
                    <TabsTrigger value="settings">Settings</TabsTrigger>
                    <TabsTrigger value="logs">Logs</TabsTrigger>
                    <TabsTrigger value="history">History</TabsTrigger>
                </TabsList>

                <TabsContent value="leads" className="mt-6">
                    {tab === "leads" && (
                        <LeadsPanel
                            // Re-read the filters when a link (e.g. "Choose which to call") changes them.
                            key={searchParams.get("filters") ?? ""}
                            endpointId={endpoint.id}
                            endpointWorkflows={{ [endpoint.id]: endpoint.workflow_id }}
                            onLeadsChanged={refreshEndpoint}
                        />
                    )}
                </TabsContent>

                <TabsContent value="setup" className="mt-6">
                    <SetupPanel endpoint={endpoint} onChange={adopt} />
                </TabsContent>

                <TabsContent value="mapping" className="mt-6 space-y-6">
                    <MappingEditor value={mapping} onChange={setMapping} endpointId={endpoint.id} />
                    <SaveBar
                        dirty={mappingDirty}
                        saving={saving}
                        onReset={() => setMapping(original.field_mapping)}
                        onSave={() => save({ field_mapping: cleanMapping(mapping) }, "Field mapping saved")}
                    />
                </TabsContent>

                <TabsContent value="settings" className="mt-6 space-y-6">
                    <EndpointForm value={settings} onChange={setSettings} />
                    {settingsErrors.length > 0 && (
                        <ul className="list-disc space-y-1 pl-5 text-sm text-destructive">
                            {settingsErrors.map((e) => (
                                <li key={e}>{e}</li>
                            ))}
                        </ul>
                    )}
                    <SaveBar
                        dirty={settingsDirty}
                        saving={saving}
                        blocked={settingsErrors.length > 0}
                        onReset={() => setSettings(original)}
                        onSave={() => {
                            // The mapping has its own tab and Save button.
                            const rest: Partial<EndpointInput> = { ...settings, name: settings.name.trim() };
                            delete rest.field_mapping;
                            save(rest, "Settings saved");
                        }}
                    />
                </TabsContent>

                <TabsContent value="logs" className="mt-6">
                    {tab === "logs" && <RequestLogsPanel endpointId={endpoint.id} />}
                </TabsContent>

                <TabsContent value="history" className="mt-6">
                    {tab === "history" && <AuditPanel endpointId={endpoint.id} />}
                </TabsContent>
            </Tabs>
        </div>
    );
}

export default function WebhookEndpointPage() {
    return (
        <Suspense fallback={<div className="container mx-auto p-6">Loading…</div>}>
            <EndpointDetail />
        </Suspense>
    );
}
