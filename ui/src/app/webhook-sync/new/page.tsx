"use client";

import { ArrowLeft, ChevronDown } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { EndpointForm, endpointFormErrors } from "@/components/webhook-sync/EndpointForm";
import { MappingEditor } from "@/components/webhook-sync/MappingEditor";
import { useAuth } from "@/lib/auth";
import { cleanMapping, createEndpoint, DEFAULT_CALL_SETTINGS, type EndpointInput } from "@/lib/webhookSync";

const EMPTY: EndpointInput = {
    name: "",
    workflow_id: 0,
    auth_type: "api_key",
    is_active: true,
    auto_call: true,
    field_mapping: { custom: {} },
    call_settings: DEFAULT_CALL_SETTINGS,
    rate_limit_per_minute: 100,
};

export default function NewWebhookEndpointPage() {
    const router = useRouter();
    const { user, loading: authLoading, redirectToLogin } = useAuth();
    // Whether someone is signed in, not the user object: a new object for the
    // same user (e.g. after a token refresh) must not re-run the fetches.
    const signedIn = Boolean(user);
    const [value, setValue] = useState<EndpointInput>(EMPTY);
    const [saving, setSaving] = useState(false);
    const [tried, setTried] = useState(false);
    const errors = endpointFormErrors(value);

    useEffect(() => {
        if (!authLoading && !signedIn) redirectToLogin();
    }, [authLoading, signedIn, redirectToLogin]);

    const create = async () => {
        setTried(true);
        if (errors.length > 0) return;
        setSaving(true);
        try {
            const endpoint = await createEndpoint({
                ...value,
                name: value.name.trim(),
                field_mapping: cleanMapping(value.field_mapping),
            });
            toast.success("Endpoint created. Add its URL and secret to your CRM.");
            router.push(`/webhook-sync/${endpoint.id}?tab=setup`);
        } catch (e) {
            toast.error(e instanceof Error ? e.message : "Failed to create the endpoint");
            setSaving(false);
        }
    };

    return (
        <div className="container mx-auto max-w-4xl space-y-6 p-6">
            <Button variant="ghost" onClick={() => router.push("/webhook-sync")}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back to Webhook Sync
            </Button>
            <div>
                <h1 className="mb-2 text-3xl font-bold">New webhook endpoint</h1>
                <p className="text-muted-foreground">
                    You&apos;ll get a URL and secret to paste into your CRM. Everything here can be changed later.
                </p>
            </div>

            <EndpointForm value={value} onChange={setValue} />

            <Collapsible>
                <CollapsibleTrigger asChild>
                    <Button variant="outline" className="w-full justify-between">
                        Field mapping (optional; common field names are detected automatically)
                        <ChevronDown className="h-4 w-4" />
                    </Button>
                </CollapsibleTrigger>
                <CollapsibleContent className="pt-4">
                    <MappingEditor
                        value={value.field_mapping}
                        onChange={(field_mapping) => setValue({ ...value, field_mapping })}
                    />
                </CollapsibleContent>
            </Collapsible>

            {tried && errors.length > 0 && (
                <ul className="list-disc space-y-1 pl-5 text-sm text-destructive">
                    {errors.map((e) => (
                        <li key={e}>{e}</li>
                    ))}
                </ul>
            )}

            <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => router.push("/webhook-sync")} disabled={saving}>
                    Cancel
                </Button>
                <Button onClick={create} disabled={saving}>
                    {saving ? "Creating…" : "Create endpoint"}
                </Button>
            </div>
        </div>
    );
}
