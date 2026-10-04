"use client";

import { Ban, PhoneCall, PhoneOff, RotateCw } from "lucide-react";
import { useState } from "react";
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
import { Button } from "@/components/ui/button";
import {
    bulkLeadAction,
    CALLABLE_AGAIN,
    callLeadAgain,
    resendLeadResult,
    stopCallingLead,
    type WebhookLead,
} from "@/lib/webhookSync";

/** What can be done to a lead from its page. */
export function LeadActions({ lead, onChanged }: { lead: WebhookLead; onChanged: () => void }) {
    const [busy, setBusy] = useState(false);

    const run = async (action: () => Promise<unknown>, done: string) => {
        setBusy(true);
        try {
            await action();
            toast.success(done);
            onChanged();
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "That didn't work");
        } finally {
            setBusy(false);
        }
    };

    const canCallAgain = CALLABLE_AGAIN.has(lead.status) && Boolean(lead.phone);
    const canStop = !["do_not_call", "invalid_number", "duplicate"].includes(lead.status);
    const canResend = lead.callback?.status === "dead_letter";
    const onHold = lead.status === "on_hold";

    if (!canCallAgain && !canStop && !canResend) return null;
    return (
        <div className="flex flex-wrap gap-2">
            {canCallAgain && (
                <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => run(() => callLeadAgain(lead.id), "Queued: the lead will be called again")}
                >
                    <PhoneCall className="mr-2 h-4 w-4" />
                    {onHold || lead.status === "skipped" ? "Call" : "Call again"}
                </Button>
            )}
            {onHold && (
                <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() =>
                        run(async () => {
                            const result = await bulkLeadAction([lead.id], "skip");
                            if (result.done.length === 0) throw new Error(result.not_done[0]?.reason ?? "Not skipped");
                        }, "Skipped: this lead won't be called")
                    }
                >
                    <PhoneOff className="mr-2 h-4 w-4" />
                    Don&apos;t call
                </Button>
            )}
            {canResend && (
                <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => run(() => resendLeadResult(lead.id), "Result is being sent to your CRM again")}
                >
                    <RotateCw className="mr-2 h-4 w-4" />
                    Resend to CRM
                </Button>
            )}
            {canStop && (
                <AlertDialog>
                    <AlertDialogTrigger asChild>
                        <Button variant="outline" size="sm" disabled={busy} className="text-destructive">
                            <Ban className="mr-2 h-4 w-4" />
                            Stop calling
                        </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                        <AlertDialogHeader>
                            <AlertDialogTitle>Stop calling {lead.name || "this lead"}?</AlertDialogTitle>
                            <AlertDialogDescription>
                                This blocks the number across your organization: this lead and any other lead with
                                the same number are marked &ldquo;Do not call&rdquo;, their queued calls are cancelled,
                                and future leads with it are never called. A call already ringing isn&apos;t cut off.
                            </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction
                                onClick={() => run(() => stopCallingLead(lead.id), "This lead won't be called again")}
                            >
                                Stop calling
                            </AlertDialogAction>
                        </AlertDialogFooter>
                    </AlertDialogContent>
                </AlertDialog>
            )}
        </div>
    );
}
