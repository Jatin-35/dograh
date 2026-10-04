"use client";

import { Eye, EyeOff, KeyRound } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { CopyButton } from "@/components/CopyButton";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
    AUTH_TYPE_LABELS,
    curlExample,
    displayUrl,
    maskSecret,
    regenerateSecret,
    type WebhookEndpoint,
} from "@/lib/webhookSync";

import { JsonBlock } from "./shared";
import { TestLeadCard } from "./TestLeadCard";

function Field({ label, value, shown, copyLabel }: { label: string; value: string; shown: string; copyLabel: string }) {
    return (
        <div className="space-y-1.5">
            <Label>{label}</Label>
            <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2">
                <code className="flex-1 break-all text-sm">{shown}</code>
                <CopyButton value={value} label={copyLabel} />
            </div>
        </div>
    );
}

export function SetupPanel({
    endpoint,
    onChange,
}: {
    endpoint: WebhookEndpoint;
    onChange: (endpoint: WebhookEndpoint) => void;
}) {
    const [reveal, setReveal] = useState(false);
    const [rotating, setRotating] = useState(false);
    const auth = AUTH_TYPE_LABELS[endpoint.auth_type];

    const rotate = async () => {
        setRotating(true);
        try {
            onChange(await regenerateSecret(endpoint.id));
            setReveal(true);
            toast.success("New secret issued. Update your CRM now; the old one no longer works.");
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to regenerate the secret");
        } finally {
            setRotating(false);
        }
    };

    return (
        <div className="space-y-6">
            <Card>
                <CardHeader>
                    <CardTitle>Connection details</CardTitle>
                    <CardDescription>
                        Paste these into your CRM&apos;s webhook settings. Authentication: <b>{auth.label}</b>. {auth.hint}
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <Field
                        label="Webhook URL (POST)"
                        value={endpoint.webhook_url}
                        shown={reveal ? endpoint.webhook_url : displayUrl(endpoint.webhook_url)}
                        copyLabel="Webhook URL"
                    />
                    <div className="space-y-1.5">
                        <Label>Secret</Label>
                        <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2">
                            <code className="flex-1 break-all text-sm">
                                {reveal ? endpoint.secret : maskSecret(endpoint.secret)}
                            </code>
                            <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6 text-muted-foreground"
                                onClick={() => setReveal((v) => !v)}
                                aria-label={reveal ? "Hide secret" : "Show secret"}
                            >
                                {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                            </Button>
                            <CopyButton value={endpoint.secret} label="Secret" />
                        </div>
                        {endpoint.auth_type === "api_key" && (
                            <p className="text-xs text-muted-foreground">
                                Send it as the header <code>X-API-Key: &lt;secret&gt;</code>.
                            </p>
                        )}
                        {endpoint.auth_type === "hmac" && (
                            <p className="text-xs text-muted-foreground">
                                Send <code>X-Botrix-Timestamp</code> (unix seconds) and{" "}
                                <code>X-Botrix-Signature</code> = hex HMAC-SHA256 of{" "}
                                <code>&quot;&lt;timestamp&gt;.&lt;raw body&gt;&quot;</code> keyed with the secret.
                            </p>
                        )}
                        {endpoint.auth_type === "url_token" && (
                            <p className="text-xs text-muted-foreground">
                                Already included in the URL above as <code>?token=</code>.
                            </p>
                        )}
                    </div>
                    <AlertDialog>
                        <AlertDialogTrigger asChild>
                            <Button variant="outline" size="sm" disabled={rotating}>
                                <KeyRound className="mr-2 h-4 w-4" />
                                {rotating ? "Regenerating…" : "Regenerate secret"}
                            </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                            <AlertDialogHeader>
                                <AlertDialogTitle>Regenerate the secret?</AlertDialogTitle>
                                <AlertDialogDescription>
                                    The current secret stops working immediately. Leads your CRM sends with it will be
                                    rejected until you update the CRM with the new one.
                                </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                                <AlertDialogCancel>Cancel</AlertDialogCancel>
                                <AlertDialogAction onClick={rotate}>Regenerate</AlertDialogAction>
                            </AlertDialogFooter>
                        </AlertDialogContent>
                    </AlertDialog>
                </CardContent>
            </Card>

            <TestLeadCard endpoint={endpoint} />

            <Card>
                <CardHeader>
                    <div className="flex items-center justify-between">
                        <div>
                            <CardTitle>Test from a terminal</CardTitle>
                            <CardDescription>
                                Run this in a terminal. The lead appears on the Leads tab, and the request on the Logs
                                tab.
                            </CardDescription>
                        </div>
                        <CopyButton value={curlExample(endpoint)} label="Test command" />
                    </div>
                </CardHeader>
                <CardContent>
                    <JsonBlock value={reveal ? curlExample(endpoint) : curlExample({ ...endpoint, secret: "<secret>", webhook_url: displayUrl(endpoint.webhook_url) })} />
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>LeadSquared</CardTitle>
                    <CardDescription>Send every new lead from LeadSquared to this agent.</CardDescription>
                </CardHeader>
                <CardContent>
                    <ol className="list-decimal space-y-2 pl-5 text-sm">
                        <li>
                            In LeadSquared, open <b>Settings → API and Webhooks → Webhooks</b> and create a webhook.
                        </li>
                        <li>
                            Event: <b>Lead creation</b> (or the event you want to call on). Method <b>POST</b>, format{" "}
                            <b>JSON</b>.
                        </li>
                        <li>Paste the webhook URL above.</li>
                        {endpoint.auth_type === "api_key" && (
                            <li>
                                Add a custom header <code>X-API-Key</code> with the secret above.
                            </li>
                        )}
                        <li>Save, then create a test lead in LeadSquared.</li>
                        <li>
                            Open the <b>Field mapping</b> tab and click <b>Use last request</b> to check its fields map
                            correctly.
                        </li>
                    </ol>
                </CardContent>
            </Card>
        </div>
    );
}
