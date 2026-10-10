"use client";

import { History, Plus, Trash2, Wand2 } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
    cleanMapping,
    type FieldMapping,
    listRequestLogs,
    type MappingPreview,
    previewMapping,
    type RequestLog,
    STANDARD_FIELDS,
} from "@/lib/webhookSync";

import { LeadStatusBadge } from "./shared";
import { ValueMapsCard } from "./ValueMapsCard";

/** "5:20 PM · 200 · {"Current":{"Phone"…" — a recent request, recognizably. */
function requestLabel(log: RequestLog): string {
    const when = new Date(log.received_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const body = (log.raw_body ?? "").replace(/\s+/g, " ").slice(0, 40);
    return `${when} · ${log.response_code} · ${body}${(log.raw_body ?? "").length > 40 ? "…" : ""}`;
}

const CONTENT_TYPES = [
    { value: "application/json", label: "JSON" },
    { value: "application/x-www-form-urlencoded", label: "Form data" },
];

interface MappingEditorProps {
    value: FieldMapping;
    onChange: (mapping: FieldMapping) => void;
    /** Enables "Use last request" (needs an existing endpoint). */
    endpointId?: number;
}

export function MappingEditor({ value, onChange, endpointId }: MappingEditorProps) {
    const listId = useId();
    const [sample, setSample] = useState("");
    const [contentType, setContentType] = useState("application/json");
    const [preview, setPreview] = useState<MappingPreview | null>(null);
    const [previewError, setPreviewError] = useState<string | null>(null);
    const [loadingLast, setLoadingLast] = useState(false);
    const latest = useRef(0);

    const runPreview = useCallback(
        async (text: string, type: string, mapping: FieldMapping) => {
            if (!text.trim()) {
                setPreview(null);
                setPreviewError(null);
                return;
            }
            const ticket = ++latest.current;
            try {
                const result = await previewMapping(text, type, cleanMapping(mapping));
                if (ticket !== latest.current) return;
                setPreview(result);
                setPreviewError(null);
            } catch (e) {
                if (ticket !== latest.current) return;
                setPreview(null);
                setPreviewError(e instanceof Error ? e.message : "Could not read the sample");
            }
        },
        [],
    );

    // Re-check the preview shortly after the sample or mapping changes.
    useEffect(() => {
        const timer = setTimeout(() => runPreview(sample, contentType, value), 400);
        return () => clearTimeout(timer);
    }, [sample, contentType, value, runPreview]);

    // Recent requests with a body (a rejected-credentials request isn't a
    // real lead), newest first, so any of them can be the sample.
    const [recent, setRecent] = useState<RequestLog[]>([]);

    const applyLog = (log: RequestLog) => {
        const isForm = (log.content_type || "").includes("x-www-form-urlencoded");
        setContentType(isForm ? "application/x-www-form-urlencoded" : "application/json");
        let text = log.raw_body ?? "";
        if (!isForm) {
            try {
                text = JSON.stringify(JSON.parse(text), null, 2);
            } catch {
                // Keep it as sent.
            }
        }
        setSample(text);
    };

    const loadLastRequest = async () => {
        if (endpointId === undefined) return;
        setLoadingLast(true);
        try {
            const { logs } = await listRequestLogs(endpointId, 20, 0);
            const usable = logs.filter((log) => log.raw_body && log.response_code !== 401);
            setRecent(usable);
            if (usable.length === 0) {
                toast.info("No request with a body yet. Send a test lead from your CRM first.");
                return;
            }
            applyLog(usable[0]);
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to load the last request");
        } finally {
            setLoadingLast(false);
        }
    };

    const setField = (key: string, path: string) => onChange({ ...value, [key]: path });
    const customEntries = Object.entries(value.custom ?? {});
    const setCustom = (entries: [string, string][]) => onChange({ ...value, custom: Object.fromEntries(entries) });

    const found = (key: string): string | null | undefined => {
        if (!preview) return undefined;
        if (key === "phone") return preview.phone ?? preview.phone_raw;
        return preview.fields[key];
    };

    return (
        <div className="space-y-6">
            <Card>
                <CardHeader>
                    <CardTitle>Sample from your CRM</CardTitle>
                    <CardDescription>
                        Paste a real lead exactly as your CRM sends it, or pull in the last request this endpoint
                        received. Nothing is stored; it only checks the mapping.
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                    <div className="flex flex-wrap items-center gap-2">
                        <Select value={contentType} onValueChange={setContentType}>
                            <SelectTrigger className="w-40">
                                <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                                {CONTENT_TYPES.map((t) => (
                                    <SelectItem key={t.value} value={t.value}>
                                        {t.label}
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                        {endpointId !== undefined && (
                            <Button variant="outline" size="sm" onClick={loadLastRequest} disabled={loadingLast}>
                                <History className="mr-2 h-4 w-4" />
                                {loadingLast ? "Loading…" : "Use last request"}
                            </Button>
                        )}
                        {recent.length > 1 && (
                            <Select
                                onValueChange={(id) => {
                                    const log = recent.find((l) => String(l.id) === id);
                                    if (log) applyLog(log);
                                }}
                            >
                                <SelectTrigger className="w-72" aria-label="Pick another recent request">
                                    <SelectValue placeholder={`Pick another (${recent.length} recent)`} />
                                </SelectTrigger>
                                <SelectContent>
                                    {recent.map((log) => (
                                        <SelectItem key={log.id} value={String(log.id)}>
                                            {requestLabel(log)}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        )}
                        <Button
                            variant="outline"
                            size="sm"
                            onClick={() =>
                                setSample(
                                    JSON.stringify(
                                        { name: "Rahul Kumar", mobile: "9876543210", city: "Patna", source: "Facebook Ads" },
                                        null,
                                        2,
                                    ),
                                )
                            }
                        >
                            <Wand2 className="mr-2 h-4 w-4" />
                            Example
                        </Button>
                    </div>
                    <Textarea
                        value={sample}
                        onChange={(e) => setSample(e.target.value)}
                        placeholder='{"FirstName": "Rahul", "Phone": "+91-9876543210", "mx_City": "Patna"}'
                        className="min-h-40 font-mono text-xs"
                    />
                    {previewError && <p className="text-sm text-destructive">{previewError}</p>}
                    {preview && (
                        <div className="flex flex-wrap items-center gap-2 text-sm">
                            <span className="text-muted-foreground">This lead would be stored as</span>
                            {preview.outcome === "rejected" ? (
                                <span className="font-medium text-destructive">rejected (not stored)</span>
                            ) : (
                                <LeadStatusBadge status={preview.outcome} />
                            )}
                            {preview.reason && <span className="text-muted-foreground">: {preview.reason}</span>}
                            {preview.lead_count > 1 && (
                                <span className="text-muted-foreground">
                                    (showing the first of {preview.lead_count} leads)
                                </span>
                            )}
                        </div>
                    )}
                </CardContent>
            </Card>

            <datalist id={listId}>
                {preview?.paths.map((p) => (
                    <option key={p.path} value={p.path}>
                        {p.value}
                    </option>
                ))}
            </datalist>

            <Card>
                <CardHeader>
                    <CardTitle>Lead fields</CardTitle>
                    <CardDescription>
                        Leave a field blank to detect it automatically (mobile, phone_number, FirstName + LastName, …).
                        Set a path like <code>Current.Phone</code> or <code>leads[0].mobile</code> when detection
                        picks the wrong value.
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                    {STANDARD_FIELDS.map(({ key, label, required }) => {
                        const detected = found(key);
                        return (
                            <div key={key} className="grid grid-cols-1 items-center gap-2 md:grid-cols-[10rem_1fr_1fr]">
                                <Label>
                                    {label}
                                    {required && <span className="text-destructive"> *</span>}
                                </Label>
                                <Input
                                    list={listId}
                                    value={value[key] ?? ""}
                                    onChange={(e) => setField(key, e.target.value)}
                                    placeholder="Auto-detect"
                                    className="font-mono text-sm"
                                />
                                <span className="truncate text-sm text-muted-foreground" title={detected ?? ""}>
                                    {detected === undefined ? "" : detected ? `→ ${detected}` : "→ not found"}
                                </span>
                            </div>
                        );
                    })}
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Extra call variables</CardTitle>
                    <CardDescription>
                        {value.only_mapped ? (
                            <>
                                Only the fields above and the variables you add here reach the agent; everything else
                                the CRM sends is left out.
                            </>
                        ) : (
                            <>
                                Every plain field the CRM sends already becomes a variable (a field called{" "}
                                <code>Lead Source</code> is <code>{"{{lead_source}}"}</code>). Add one here to give a
                                nested field a name your agent&apos;s prompt uses.
                            </>
                        )}
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                    <div className="flex items-start justify-between gap-4 rounded-md border p-3">
                        <div className="space-y-0.5">
                            <Label htmlFor={`${listId}-only-mapped`}>Only keep mapped fields</Label>
                            <p className="text-xs text-muted-foreground">
                                Drop every other field the CRM sends (LeadSquared sends about 70). The original
                                payload stays on the lead&apos;s page for 30 days.
                            </p>
                        </div>
                        <Switch
                            id={`${listId}-only-mapped`}
                            checked={Boolean(value.only_mapped)}
                            onCheckedChange={(checked) => onChange({ ...value, only_mapped: checked })}
                        />
                    </div>
                    {customEntries.map(([name, path], index) => (
                        <div key={index} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
                            <Input
                                value={name}
                                onChange={(e) => {
                                    const next = [...customEntries];
                                    next[index] = [e.target.value, path];
                                    setCustom(next);
                                }}
                                placeholder="Variable name, e.g. product"
                            />
                            <Input
                                list={listId}
                                value={path}
                                onChange={(e) => {
                                    const next = [...customEntries];
                                    next[index] = [name, e.target.value];
                                    setCustom(next);
                                }}
                                placeholder="Path, e.g. data.product"
                                className="font-mono text-sm"
                            />
                            <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => setCustom(customEntries.filter((_, i) => i !== index))}
                                aria-label="Remove variable"
                            >
                                <Trash2 className="h-4 w-4" />
                            </Button>
                        </div>
                    ))}
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setCustom([...customEntries, [`field_${customEntries.length + 1}`, ""]])}
                        disabled={customEntries.length >= 50}
                    >
                        <Plus className="mr-2 h-4 w-4" />
                        Add variable
                    </Button>

                    {preview && Object.keys(preview.variables).length > 0 && (
                        <div className="pt-2">
                            <p className="mb-2 text-sm font-medium">Variables the agent will get</p>
                            <div className="flex flex-wrap gap-2">
                                {Object.entries(preview.variables).map(([k, v]) => (
                                    <span key={k} className="rounded bg-muted px-2 py-1 font-mono text-xs" title={v}>
                                        {`{{${k}}}`} = {v.length > 40 ? `${v.slice(0, 40)}…` : v}
                                    </span>
                                ))}
                            </div>
                        </div>
                    )}
                </CardContent>
            </Card>

            <ValueMapsCard
                value={value.value_maps}
                onChange={(value_maps) => onChange({ ...value, value_maps })}
                fieldSuggestions={Object.keys(preview?.variables ?? {}).filter((name) => name !== "phone_number")}
                previewValue={(field) => {
                    if (!preview) return undefined;
                    const name = field.trim().toLowerCase().replace(/[^0-9a-z]+/g, "_").replace(/^_+|_+$/g, "");
                    return preview.variables[name] ?? preview.fields[name] ?? undefined;
                }}
            />
        </div>
    );
}
