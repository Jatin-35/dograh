"use client";

import { useEffect, useState } from "react";

import {
    getWorkflowsSummaryApiV1WorkflowSummaryGet,
    listTelephonyConfigurationsApiV1OrganizationsTelephonyConfigsGet,
} from "@/client/sdk.gen";
import type { TelephonyConfigurationListItem, WorkflowSummaryResponse } from "@/client/types.gen";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/lib/auth";
import {
    AUTH_TYPE_LABELS,
    type AuthType,
    type CallSettings,
    DAY_LABELS,
    type EndpointInput,
    parseEmailList,
} from "@/lib/webhookSync";

// The campaign engine retries unanswered and busy calls; a failed call (a
// provider or number error) is not retried, as dialing again rarely helps.
const RETRY_STATUSES: { value: "no_answer" | "busy"; label: string }[] = [
    { value: "no_answer", label: "No answer" },
    { value: "busy", label: "Busy" },
];

function intOr(text: string, fallback: number): number {
    const n = parseInt(text, 10);
    return Number.isNaN(n) ? fallback : n;
}

/** Problems that would make the API reject the endpoint, for inline display. */
export function endpointFormErrors(value: EndpointInput): string[] {
    const errors: string[] = [];
    if (!value.name.trim()) errors.push("Give the endpoint a name");
    if (!value.workflow_id) errors.push("Choose the agent that calls these leads");
    const hours = value.call_settings.calling_hours;
    if (hours.start >= hours.end) errors.push("Calling hours must end after they start");
    if (hours.days.length === 0) errors.push("Pick at least one calling day");
    const url = value.call_settings.callback_url?.trim();
    if (url && !url.toLowerCase().startsWith("https://")) errors.push("The callback URL must start with https://");
    if ((value.call_settings.alert_emails ?? []).length > 5) errors.push("At most 5 alert emails");
    return errors;
}

/** Comma-separated alert emails, kept as typed while editing. */
function AlertEmailsInput({ value, onChange }: { value: string[]; onChange: (emails: string[]) => void }) {
    const [text, setText] = useState(value.join(", "));
    const { invalid } = parseEmailList(text);
    return (
        <div className="space-y-1.5">
            <Label htmlFor="ws-alert-emails">Alert emails</Label>
            <Input
                id="ws-alert-emails"
                value={text}
                onChange={(e) => {
                    setText(e.target.value);
                    onChange(parseEmailList(e.target.value).emails);
                }}
                placeholder="ops@yourcompany.com, manager@yourcompany.com"
            />
            {invalid.length > 0 ? (
                <p className="text-xs text-destructive">Not a valid email: {invalid.join(", ")}</p>
            ) : (
                <p className="text-xs text-muted-foreground">
                    Up to 5. Leave empty to alert the person who created this endpoint. Alerts also appear in the
                    endpoint&apos;s History.
                </p>
            )}
        </div>
    );
}

export function EndpointForm({
    value,
    onChange,
}: {
    value: EndpointInput;
    onChange: (value: EndpointInput) => void;
}) {
    const { isAuthenticated, loading: authLoading } = useAuth();
    const [agents, setAgents] = useState<WorkflowSummaryResponse[] | null>(null);
    const [telephony, setTelephony] = useState<TelephonyConfigurationListItem[] | null>(null);

    useEffect(() => {
        if (authLoading || !isAuthenticated) return;
        getWorkflowsSummaryApiV1WorkflowSummaryGet({ query: { status: "active" } })
            .then((response) => setAgents(response.data ?? []))
            .catch(() => setAgents([]));
        listTelephonyConfigurationsApiV1OrganizationsTelephonyConfigsGet()
            .then((response) => setTelephony(response.data?.configurations ?? []))
            .catch(() => setTelephony([]));
    }, [authLoading, isAuthenticated]);

    const set = <K extends keyof EndpointInput>(key: K, v: EndpointInput[K]) => onChange({ ...value, [key]: v });
    const setCall = (changes: Partial<CallSettings>) =>
        onChange({ ...value, call_settings: { ...value.call_settings, ...changes } });
    const cs = value.call_settings;

    return (
        <div className="space-y-6">
            <Card>
                <CardHeader>
                    <CardTitle>Endpoint</CardTitle>
                    <CardDescription>Which CRM this is, and which agent calls its leads.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <div className="space-y-1.5">
                        <Label htmlFor="ws-name">Name</Label>
                        <Input
                            id="ws-name"
                            value={value.name}
                            maxLength={255}
                            onChange={(e) => set("name", e.target.value)}
                            placeholder="LeadSquared – Real estate leads"
                        />
                    </div>
                    <div className="space-y-1.5">
                        <Label>Agent</Label>
                        <Select
                            value={value.workflow_id ? String(value.workflow_id) : ""}
                            onValueChange={(v) => set("workflow_id", Number(v))}
                        >
                            <SelectTrigger>
                                <SelectValue placeholder="Select an agent" />
                            </SelectTrigger>
                            <SelectContent>
                                {agents === null ? (
                                    <SelectItem value="loading" disabled>
                                        Loading agents…
                                    </SelectItem>
                                ) : agents.length === 0 ? (
                                    <SelectItem value="none" disabled>
                                        No agents found
                                    </SelectItem>
                                ) : (
                                    agents.map((agent) => (
                                        <SelectItem key={agent.id} value={String(agent.id)}>
                                            {agent.name} (#{agent.id})
                                        </SelectItem>
                                    ))
                                )}
                            </SelectContent>
                        </Select>
                    </div>
                    <div className="space-y-2">
                        <Label>How your CRM proves it&apos;s allowed</Label>
                        <RadioGroup
                            value={value.auth_type}
                            onValueChange={(v) => set("auth_type", v as AuthType)}
                            className="space-y-2"
                        >
                            {(Object.keys(AUTH_TYPE_LABELS) as AuthType[]).map((type) => (
                                <label key={type} className="flex cursor-pointer items-start gap-3 rounded-md border p-3">
                                    <RadioGroupItem value={type} className="mt-0.5" />
                                    <div>
                                        <p className="text-sm font-medium">{AUTH_TYPE_LABELS[type].label}</p>
                                        <p className="text-xs text-muted-foreground">{AUTH_TYPE_LABELS[type].hint}</p>
                                    </div>
                                </label>
                            ))}
                        </RadioGroup>
                    </div>
                    <div className="flex items-center justify-between rounded-md border p-3">
                        <div>
                            <p className="text-sm font-medium">Active</p>
                            <p className="text-xs text-muted-foreground">
                                A paused endpoint still receives leads and keeps them on hold, uncalled. After you
                                resume it, you choose which of them to call.
                            </p>
                        </div>
                        <Switch checked={value.is_active} onCheckedChange={(v) => set("is_active", v)} />
                    </div>
                    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-rate">Rate limit (requests per minute)</Label>
                            <Input
                                id="ws-rate"
                                type="number"
                                min={1}
                                max={1000}
                                value={value.rate_limit_per_minute}
                                onChange={(e) =>
                                    set("rate_limit_per_minute", Math.min(1000, Math.max(1, intOr(e.target.value, 100))))
                                }
                            />
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-dedupe">Treat the same number as a duplicate for (hours)</Label>
                            <Input
                                id="ws-dedupe"
                                type="number"
                                min={0}
                                max={720}
                                value={cs.dedupe_window_hours}
                                onChange={(e) =>
                                    setCall({ dedupe_window_hours: Math.min(720, Math.max(0, intOr(e.target.value, 24))) })
                                }
                            />
                            <p className="text-xs text-muted-foreground">0 turns duplicate detection off.</p>
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-reenquiry">Same CRM lead counts as a new enquiry after (days)</Label>
                            <Input
                                id="ws-reenquiry"
                                type="number"
                                min={1}
                                max={365}
                                value={cs.reenquiry_days ?? 30}
                                onChange={(e) =>
                                    setCall({ reenquiry_days: Math.min(365, Math.max(1, intOr(e.target.value, 30))) })
                                }
                            />
                            <p className="text-xs text-muted-foreground">
                                Before that, a repeat of the same CRM lead isn&apos;t called again, unless it brings a
                                corrected number.
                            </p>
                        </div>
                    </div>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Calling</CardTitle>
                    <CardDescription>
                        When and how the agent calls each new lead. Calls go out through the campaign engine, so your
                        numbers&apos; channels, the organization&apos;s call limit and your wallet balance all apply.
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <div className="space-y-1.5">
                        <Label>Call from</Label>
                        <Select
                            value={cs.telephony_configuration_id ? String(cs.telephony_configuration_id) : "default"}
                            onValueChange={(v) =>
                                setCall({ telephony_configuration_id: v === "default" ? null : Number(v) })
                            }
                        >
                            <SelectTrigger>
                                <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="default">Organization default</SelectItem>
                                {(telephony ?? []).map((cfg) => (
                                    <SelectItem key={cfg.id} value={String(cfg.id)}>
                                        {cfg.name} ({cfg.provider}
                                        {cfg.phone_number_count != null ? `, ${cfg.phone_number_count} numbers` : ""})
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                        {telephony !== null && telephony.length === 0 && (
                            <p className="text-xs text-destructive">
                                No telephony configuration yet. Add one under Telephony, or leads can&apos;t be called.
                            </p>
                        )}
                    </div>
                    <div className="flex items-center justify-between rounded-md border p-3">
                        <div>
                            <p className="text-sm font-medium">Call new leads automatically</p>
                            <p className="text-xs text-muted-foreground">
                                Off: leads are only stored, and calls waiting to go out are cancelled.
                            </p>
                        </div>
                        <Switch checked={value.auto_call} onCheckedChange={(v) => set("auto_call", v)} />
                    </div>
                    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-delay">Wait before calling (minutes)</Label>
                            <Input
                                id="ws-delay"
                                type="number"
                                min={0}
                                max={1440}
                                value={cs.delay_minutes}
                                onChange={(e) =>
                                    setCall({ delay_minutes: Math.min(1440, Math.max(0, intOr(e.target.value, 0))) })
                                }
                            />
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-start">Calling hours from</Label>
                            <Input
                                id="ws-start"
                                type="time"
                                value={cs.calling_hours.start}
                                onChange={(e) =>
                                    setCall({ calling_hours: { ...cs.calling_hours, start: e.target.value || "09:00" } })
                                }
                            />
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-end">to ({cs.calling_hours.timezone})</Label>
                            <Input
                                id="ws-end"
                                type="time"
                                value={cs.calling_hours.end}
                                onChange={(e) =>
                                    setCall({ calling_hours: { ...cs.calling_hours, end: e.target.value || "21:00" } })
                                }
                            />
                        </div>
                    </div>
                    <div className="space-y-1.5">
                        <Label>Calling days</Label>
                        <div className="flex flex-wrap gap-3">
                            {DAY_LABELS.map((day, index) => (
                                <label key={day} className="flex items-center gap-1.5 text-sm">
                                    <Checkbox
                                        checked={cs.calling_hours.days.includes(index)}
                                        onCheckedChange={(checked) => {
                                            const days = checked
                                                ? [...cs.calling_hours.days, index]
                                                : cs.calling_hours.days.filter((d) => d !== index);
                                            setCall({
                                                calling_hours: { ...cs.calling_hours, days: [...new Set(days)].sort() },
                                            });
                                        }}
                                    />
                                    {day}
                                </label>
                            ))}
                        </div>
                    </div>
                    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-attempts">Call attempts per lead (including the first)</Label>
                            <Input
                                id="ws-attempts"
                                type="number"
                                min={1}
                                max={10}
                                value={cs.retries.max_attempts}
                                onChange={(e) =>
                                    setCall({
                                        retries: {
                                            ...cs.retries,
                                            max_attempts: Math.min(10, Math.max(1, intOr(e.target.value, 3))),
                                        },
                                    })
                                }
                            />
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="ws-gap">Minutes between attempts</Label>
                            <Input
                                id="ws-gap"
                                type="number"
                                min={5}
                                max={1440}
                                value={cs.retries.gap_minutes}
                                onChange={(e) =>
                                    setCall({
                                        retries: {
                                            ...cs.retries,
                                            gap_minutes: Math.min(1440, Math.max(5, intOr(e.target.value, 30))),
                                        },
                                    })
                                }
                            />
                        </div>
                    </div>
                    <div className="space-y-1.5">
                        <Label>Retry when the call is</Label>
                        <div className="flex flex-wrap gap-3">
                            {RETRY_STATUSES.map(({ value: status, label }) => (
                                <label key={status} className="flex items-center gap-1.5 text-sm">
                                    <Checkbox
                                        checked={cs.retries.on_statuses.includes(status)}
                                        onCheckedChange={(checked) =>
                                            setCall({
                                                retries: {
                                                    ...cs.retries,
                                                    on_statuses: checked
                                                        ? [...new Set([...cs.retries.on_statuses, status])]
                                                        : cs.retries.on_statuses.filter((s) => s !== status),
                                                },
                                            })
                                        }
                                    />
                                    {label}
                                </label>
                            ))}
                        </div>
                    </div>
                </CardContent>
            </Card>
            <Card>
                <CardHeader>
                    <CardTitle>Send results to your CRM</CardTitle>
                    <CardDescription>
                        Optional. When a lead&apos;s calling is finished (connected, or no attempts left), we POST the
                        result to this URL: the lead, its status and disposition, what the agent collected, and links
                        to the recording and transcript. Failed deliveries are retried.
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-1.5">
                    <Label htmlFor="ws-callback">Callback URL (https)</Label>
                    <Input
                        id="ws-callback"
                        type="url"
                        value={cs.callback_url ?? ""}
                        maxLength={2048}
                        onChange={(e) => setCall({ callback_url: e.target.value.trim() || null })}
                        placeholder="https://your-crm.example.com/botrix/call-result"
                    />
                    <p className="text-xs text-muted-foreground">
                        Each request carries the header <code>X-Botrix-Secret</code> with this endpoint&apos;s secret,
                        so your CRM can check it came from us, and <code>X-Dograh-Delivery-Id</code> to ignore
                        repeats.
                    </p>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Alerts</CardTitle>
                    <CardDescription>
                        We email these people when this endpoint needs attention: your CRM&apos;s requests keep
                        failing, leads can&apos;t be called (for example an empty wallet), calling was paused after
                        too many failed calls, or results can&apos;t be sent to your CRM. At most once every 6 hours
                        per problem.
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    <AlertEmailsInput
                        value={cs.alert_emails ?? []}
                        onChange={(emails) => setCall({ alert_emails: emails })}
                    />
                </CardContent>
            </Card>
        </div>
    );
}
