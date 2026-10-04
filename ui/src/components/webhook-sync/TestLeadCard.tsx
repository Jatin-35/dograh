"use client";

import { Send } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { sendTestLead, type TestLeadResult, type WebhookEndpoint } from "@/lib/webhookSync";

/** What a test lead's reply means, in words. */
export function describeTestResult(result: TestLeadResult): { ok: boolean; text: string } {
    const body = result.body as Record<string, unknown>;
    if (result.status_code !== 200) {
        const error = typeof body.error === "string" ? body.error : `HTTP ${result.status_code}`;
        return { ok: false, text: `Not accepted: ${error}` };
    }
    if (body.invalid) return { ok: false, text: "Stored, but not a valid Indian mobile number, so it won't be called." };
    if (body.duplicates) return { ok: false, text: "Stored as a duplicate: this number came in recently, so it won't be called again." };
    if (body.replayed) return { ok: true, text: "Already received earlier; nothing new was stored." };
    return { ok: true, text: "Received. With auto-call on, this number will be called within calling hours." };
}

export function TestLeadCard({ endpoint }: { endpoint: WebhookEndpoint }) {
    const [phone, setPhone] = useState("");
    const [name, setName] = useState("");
    const [sending, setSending] = useState(false);
    const [result, setResult] = useState<TestLeadResult | null>(null);
    const [error, setError] = useState<string | null>(null);

    const send = async () => {
        setSending(true);
        setError(null);
        try {
            setResult(await sendTestLead(endpoint.id, phone.trim(), name.trim() || undefined));
        } catch (e) {
            setResult(null);
            setError(e instanceof Error ? e.message : "Could not send the test lead");
        } finally {
            setSending(false);
        }
    };

    const outcome = result ? describeTestResult(result) : null;
    return (
        <Card>
            <CardHeader>
                <CardTitle>Send a test lead</CardTitle>
                <CardDescription>
                    Sends a lead to this endpoint exactly as your CRM would, with its secret. Use your own number
                    to get a real test call{endpoint.auto_call ? "" : " (auto-call is off, so it will only be stored)"}.
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
                <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
                    <div className="space-y-1.5">
                        <Label htmlFor="ws-test-phone">Phone number</Label>
                        <Input
                            id="ws-test-phone"
                            inputMode="tel"
                            value={phone}
                            onChange={(e) => setPhone(e.target.value)}
                            placeholder="98765 43210"
                        />
                    </div>
                    <div className="space-y-1.5">
                        <Label htmlFor="ws-test-name">Name (optional)</Label>
                        <Input
                            id="ws-test-name"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="Test Lead"
                        />
                    </div>
                    <Button onClick={send} disabled={sending || !phone.trim()}>
                        <Send className="mr-2 h-4 w-4" />
                        {sending ? "Sending…" : "Send test lead"}
                    </Button>
                </div>
                {error && <p className="text-sm text-destructive">{error}</p>}
                {outcome && (
                    <p className={`text-sm ${outcome.ok ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}`}>
                        {outcome.text}
                        {result?.lead_id && (
                            <>
                                {" "}
                                <Link href={`/webhook-sync/leads/${result.lead_id}`} className="font-medium underline">
                                    Open lead #{result.lead_id}
                                </Link>
                            </>
                        )}
                    </p>
                )}
            </CardContent>
        </Card>
    );
}
