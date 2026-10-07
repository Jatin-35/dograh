"use client";

import { Loader2, Mail, RotateCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    listOrganizationInvitations,
    type OrganizationInvitation,
    revokeOrganizationInvitation,
    sendOrganizationInvitation,
} from "@/lib/superadminOrganizations";

interface Props {
    organization: { id: number; name: string | null } | null;
    onClose: () => void;
}

function formatExpiry(iso: string | null): string {
    if (!iso) return "";
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
        ? ""
        : `expires ${date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;
}

/** Send, resend and revoke invitations into an existing organization. */
export function OrganizationInvitationsDialog({ organization, onClose }: Props) {
    const [email, setEmail] = useState("");
    const [invitations, setInvitations] = useState<OrganizationInvitation[]>([]);
    const [linkOpensAt, setLinkOpensAt] = useState("");
    const [loading, setLoading] = useState(false);
    const [loadError, setLoadError] = useState("");
    const [sendError, setSendError] = useState("");
    const [busy, setBusy] = useState<string | null>(null); // "send" or an invitation id

    const orgId = organization?.id;

    const load = useCallback(async () => {
        if (orgId == null) return;
        setLoading(true);
        setLoadError("");
        try {
            const result = await listOrganizationInvitations(orgId);
            setInvitations(result.invitations);
            setLinkOpensAt(result.link_opens_at);
        } catch (err) {
            setLoadError(err instanceof Error ? err.message : "Failed to load invitations");
        } finally {
            setLoading(false);
        }
    }, [orgId]);

    useEffect(() => {
        setEmail("");
        setSendError("");
        setInvitations([]);
        void load();
    }, [load]);

    const send = async (address: string, key: string) => {
        if (orgId == null) return;
        setBusy(key);
        setSendError("");
        try {
            const result = await sendOrganizationInvitation(orgId, address);
            toast.success(`Invitation sent to ${result.email}.`);
            if (key === "send") setEmail("");
            await load();
        } catch (err) {
            const message = err instanceof Error ? err.message : "Failed to send the invitation";
            if (key === "send") setSendError(message);
            else toast.error(message);
        } finally {
            setBusy(null);
        }
    };

    const revoke = async (invitation: OrganizationInvitation) => {
        if (orgId == null) return;
        setBusy(invitation.id);
        try {
            await revokeOrganizationInvitation(orgId, invitation.id);
            toast.success(`Invitation to ${invitation.email} revoked.`);
            await load();
        } catch (err) {
            toast.error(err instanceof Error ? err.message : "Failed to revoke the invitation");
        } finally {
            setBusy(null);
        }
    };

    return (
        <Dialog open={organization != null} onOpenChange={(open) => !open && !busy && onClose()}>
            <DialogContent className="sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <Mail className="h-5 w-5" />
                        Invite to {organization?.name || "organization"}
                    </DialogTitle>
                    <DialogDescription>
                        They get an email with a link to join. Sending again to someone already invited replaces their
                        old link.
                    </DialogDescription>
                </DialogHeader>

                <form
                    className="space-y-2"
                    onSubmit={(e) => {
                        e.preventDefault();
                        if (email.trim()) void send(email.trim(), "send");
                    }}
                >
                    <Label htmlFor="invite-email">Email</Label>
                    <div className="flex gap-2">
                        <Input
                            id="invite-email"
                            type="email"
                            placeholder="name@company.com"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            disabled={busy != null}
                        />
                        <Button type="submit" disabled={!email.trim() || busy != null}>
                            {busy === "send" ? <Loader2 className="h-4 w-4 animate-spin" /> : "Send invite"}
                        </Button>
                    </div>
                    {sendError && <p className="text-sm text-destructive">{sendError}</p>}
                    {linkOpensAt && (
                        <p className="text-xs text-muted-foreground">
                            Links open at <span className="font-mono">{new URL(linkOpensAt).host}</span>
                        </p>
                    )}
                </form>

                <div className="space-y-2 pt-2">
                    <p className="text-sm font-medium">Pending invitations</p>
                    {loading ? (
                        <div className="flex items-center gap-2 text-sm text-muted-foreground">
                            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
                        </div>
                    ) : loadError ? (
                        <p className="text-sm text-destructive">{loadError}</p>
                    ) : invitations.length === 0 ? (
                        <p className="text-sm text-muted-foreground">None.</p>
                    ) : (
                        <ul className="divide-y rounded-md border">
                            {invitations.map((invitation) => (
                                <li key={invitation.id} className="flex items-center justify-between gap-2 px-3 py-2">
                                    <div className="min-w-0">
                                        <p className="truncate text-sm">{invitation.email}</p>
                                        <p className="text-xs text-muted-foreground">{formatExpiry(invitation.expires_at)}</p>
                                    </div>
                                    <div className="flex shrink-0 gap-1">
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            disabled={busy != null || !invitation.email}
                                            onClick={() => invitation.email && void send(invitation.email, invitation.id)}
                                            aria-label={`Resend to ${invitation.email}`}
                                        >
                                            {busy === invitation.id ? (
                                                <Loader2 className="h-4 w-4 animate-spin" />
                                            ) : (
                                                <RotateCw className="h-4 w-4" />
                                            )}
                                            <span className="ml-1">Resend</span>
                                        </Button>
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            disabled={busy != null}
                                            onClick={() => void revoke(invitation)}
                                            aria-label={`Revoke invitation to ${invitation.email}`}
                                        >
                                            <Trash2 className="h-4 w-4" />
                                        </Button>
                                    </div>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </DialogContent>
        </Dialog>
    );
}
