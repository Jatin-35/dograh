"use client";

import { ArrowLeft, Loader2, Minus, Plus, Save } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
    adjustWalletApiV1WalletOrganizationsOrganizationIdAdjustPost,
    getWalletSettingsApiV1WalletOrganizationsOrganizationIdGet,
    listOrganizationsApiV1SuperuserOrganizationsGet,
    listOrganizationWalletTransactionsApiV1WalletOrganizationsOrganizationIdTransactionsGet,
    listWorkflowsApiV1SuperuserWorkflowsGet,
    topupWalletApiV1WalletOrganizationsOrganizationIdTopupPost,
    updateWalletSettingsApiV1WalletOrganizationsOrganizationIdPatch,
    updateWorkflowBillingSettingsApiV1WalletWorkflowsWorkflowIdBillingSettingsPatch,
} from "@/client/sdk.gen";
import type {
    SuperuserOrganizationResponse,
    SuperuserWorkflowResponse,
    WalletSettingsResponse,
    WalletTransactionResponse,
} from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

const TRANSACTIONS_PAGE_SIZE = 20;

function formatMoney(amount: string | number, currency: string): string {
    const value = Number(amount);
    if (Number.isNaN(value)) return amount.toString();
    try {
        return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(value);
    } catch {
        return `${value.toFixed(2)} ${currency}`;
    }
}

function formatDate(value: string): string {
    // Pinned to en-US regardless of the viewer's browser locale, so the
    // ledger reads the same for everyone (e.g. not "30 Jul 2026, 21:14").
    return new Date(value).toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
    });
}

// Per-minute pulse sizes (seconds). "0" is pay-as-you-go: billed exactly per
// second. Must match WALLET_PULSE_SECONDS_OPTIONS in api/db/wallet_client.py.
const PULSE_OPTIONS = [
    { value: "0", label: "Pay as you go" },
    { value: "15", label: "15 sec" },
    { value: "30", label: "30 sec" },
    { value: "45", label: "45 sec" },
    { value: "60", label: "60 sec" },
];

const TX_TYPE_LABEL: Record<string, string> = {
    topup: "Top-up",
    debit: "Call charge",
    adjustment: "Manual adjustment",
    refund: "Refund",
    campaign_reserve: "Campaign reserved",
    campaign_cost: "Campaign call cost",
    campaign_reconcile: "Campaign reconciled",
};

export default function SuperadminWalletDetailPage() {
    const { user, loading: authLoading } = useAuth();
    const params = useParams();
    const organizationId = Number(params.organizationId);

    const [organization, setOrganization] = useState<SuperuserOrganizationResponse | null>(null);
    const [wallet, setWallet] = useState<WalletSettingsResponse | null>(null);
    const [workflows, setWorkflows] = useState<SuperuserWorkflowResponse[]>([]);
    const [transactions, setTransactions] = useState<WalletTransactionResponse[]>([]);
    const [totalTransactions, setTotalTransactions] = useState(0);
    const [page, setPage] = useState(1);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    // Settings form state
    const [currency, setCurrency] = useState("INR");
    const [creditLimit, setCreditLimit] = useState("");
    const [isSavingSettings, setIsSavingSettings] = useState(false);
    const [isTogglingWallet, setIsTogglingWallet] = useState(false);

    // Topup / adjust dialog state
    const [topupOpen, setTopupOpen] = useState(false);
    const [topupAmount, setTopupAmount] = useState("");
    const [topupNote, setTopupNote] = useState("");
    const [isTopupSubmitting, setIsTopupSubmitting] = useState(false);

    const [adjustOpen, setAdjustOpen] = useState(false);
    const [adjustAmount, setAdjustAmount] = useState("");
    const [adjustNote, setAdjustNote] = useState("");
    const [isAdjustSubmitting, setIsAdjustSubmitting] = useState(false);

    // Per-workflow billing edit state. Exactly one pricing mode applies per
    // agent (billing_mode) — per_minute reads durationDrafts + rateDrafts +
    // pulseDrafts, per_call reads perCallRateDrafts.
    const [modeDrafts, setModeDrafts] = useState<Record<number, string>>({});
    const [durationDrafts, setDurationDrafts] = useState<Record<number, string>>({});
    const [rateDrafts, setRateDrafts] = useState<Record<number, string>>({});
    const [pulseDrafts, setPulseDrafts] = useState<Record<number, string>>({});
    const [perCallRateDrafts, setPerCallRateDrafts] = useState<Record<number, string>>({});
    const [savingWorkflowId, setSavingWorkflowId] = useState<number | null>(null);

    const fetchAll = useCallback(async () => {
        setIsLoading(true);
        setError("");

        const [orgsRes, walletRes, workflowsRes] = await Promise.all([
            listOrganizationsApiV1SuperuserOrganizationsGet(),
            getWalletSettingsApiV1WalletOrganizationsOrganizationIdGet({
                path: { organization_id: organizationId },
            }),
            listWorkflowsApiV1SuperuserWorkflowsGet(),
        ]);

        if (walletRes.error) {
            setError(detailFromError(walletRes.error, "Failed to load wallet"));
            setIsLoading(false);
            return;
        }

        if (orgsRes.data) {
            const org = orgsRes.data.organizations.find((o) => o.id === organizationId) ?? null;
            setOrganization(org);
        }
        if (walletRes.data) {
            setWallet(walletRes.data);
            setCurrency(walletRes.data.wallet_currency);
            setCreditLimit(walletRes.data.credit_limit);
        }
        if (workflowsRes.data) {
            const orgWorkflows = workflowsRes.data.workflows.filter(
                (wf) => wf.organization_id === organizationId,
            );
            setWorkflows(orgWorkflows);
            setModeDrafts(
                Object.fromEntries(orgWorkflows.map((wf) => [wf.id, wf.billing_mode])),
            );
            setDurationDrafts(
                Object.fromEntries(
                    orgWorkflows.map((wf) => [wf.id, wf.avg_call_duration_minutes ?? ""]),
                ),
            );
            setRateDrafts(
                Object.fromEntries(
                    orgWorkflows.map((wf) => [wf.id, wf.price_per_minute ?? ""]),
                ),
            );
            setPerCallRateDrafts(
                Object.fromEntries(
                    orgWorkflows.map((wf) => [wf.id, wf.price_per_call ?? ""]),
                ),
            );
            setPulseDrafts(
                Object.fromEntries(
                    orgWorkflows.map((wf) => [wf.id, String(wf.pulse_seconds ?? 0)]),
                ),
            );
        }

        setIsLoading(false);
    }, [organizationId]);

    const fetchTransactions = useCallback(
        async (targetPage: number) => {
            const response = await listOrganizationWalletTransactionsApiV1WalletOrganizationsOrganizationIdTransactionsGet(
                {
                    path: { organization_id: organizationId },
                    query: { page: targetPage, limit: TRANSACTIONS_PAGE_SIZE },
                },
            );
            if (response.data) {
                setTransactions(response.data.transactions);
                setTotalTransactions(response.data.total_count);
                setPage(targetPage);
            }
        },
        [organizationId],
    );

    useEffect(() => {
        if (authLoading || !user || hasFetched.current || Number.isNaN(organizationId)) return;
        hasFetched.current = true;
        fetchAll();
        fetchTransactions(1);
    }, [authLoading, user, organizationId, fetchAll, fetchTransactions]);

    const handleSaveSettings = async () => {
        setIsSavingSettings(true);
        try {
            const response = await updateWalletSettingsApiV1WalletOrganizationsOrganizationIdPatch({
                path: { organization_id: organizationId },
                body: {
                    wallet_currency: currency,
                    credit_limit: creditLimit.trim() || undefined,
                },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to update wallet settings"));
                return;
            }
            if (response.data) {
                setWallet(response.data);
                toast.success("Wallet settings updated");
            }
        } finally {
            setIsSavingSettings(false);
        }
    };

    const handleToggleWalletEnabled = async (nextEnabled: boolean) => {
        setIsTogglingWallet(true);
        try {
            const response = await updateWalletSettingsApiV1WalletOrganizationsOrganizationIdPatch({
                path: { organization_id: organizationId },
                body: { wallet_enabled: nextEnabled },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to update wallet status"));
                return;
            }
            if (response.data) {
                setWallet(response.data);
                toast.success(nextEnabled ? "Wallet enabled" : "Wallet disabled");
            }
        } finally {
            setIsTogglingWallet(false);
        }
    };

    const handleTopup = async () => {
        setIsTopupSubmitting(true);
        try {
            const response = await topupWalletApiV1WalletOrganizationsOrganizationIdTopupPost({
                path: { organization_id: organizationId },
                body: { amount: topupAmount, note: topupNote.trim() || undefined },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to top up wallet"));
                return;
            }
            if (response.data) {
                setWallet(response.data);
                toast.success("Wallet topped up");
                setTopupOpen(false);
                setTopupAmount("");
                setTopupNote("");
                fetchTransactions(1);
            }
        } finally {
            setIsTopupSubmitting(false);
        }
    };

    const handleAdjust = async () => {
        setIsAdjustSubmitting(true);
        try {
            const response = await adjustWalletApiV1WalletOrganizationsOrganizationIdAdjustPost({
                path: { organization_id: organizationId },
                body: { amount: adjustAmount, note: adjustNote },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to adjust wallet"));
                return;
            }
            if (response.data) {
                setWallet(response.data);
                toast.success("Wallet adjusted");
                setAdjustOpen(false);
                setAdjustAmount("");
                setAdjustNote("");
                fetchTransactions(1);
            }
        } finally {
            setIsAdjustSubmitting(false);
        }
    };

    const handleSaveWorkflowBilling = async (workflowId: number) => {
        const mode = modeDrafts[workflowId] ?? "per_minute";
        const durationDraft = durationDrafts[workflowId]?.trim();
        const rateDraft = rateDrafts[workflowId]?.trim();
        const perCallRateDraft = perCallRateDrafts[workflowId]?.trim();
        const pulseDraft = Number(pulseDrafts[workflowId] ?? "0");
        // Only the active mode's field(s) are meaningful to send — the
        // other mode's draft is left as-is in the UI but not submitted.
        // Per-minute always has a pulse selected, so a pulse-only change
        // is still worth saving.
        if (mode === "per_call" && !perCallRateDraft) return;
        setSavingWorkflowId(workflowId);
        try {
            const response = await updateWorkflowBillingSettingsApiV1WalletWorkflowsWorkflowIdBillingSettingsPatch({
                path: { workflow_id: workflowId },
                body: {
                    billing_mode: mode,
                    ...(mode === "per_call"
                        ? { price_per_call: perCallRateDraft || undefined }
                        : {
                              avg_call_duration_minutes: durationDraft || undefined,
                              price_per_minute: rateDraft || undefined,
                              pulse_seconds: pulseDraft,
                          }),
                },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to update agent billing"));
                return;
            }
            if (response.data) {
                setWorkflows((prev) =>
                    prev.map((wf) =>
                        wf.id === workflowId
                            ? {
                                  ...wf,
                                  billing_mode: response.data!.billing_mode,
                                  avg_call_duration_minutes: response.data!.avg_call_duration_minutes,
                                  price_per_minute: response.data!.price_per_minute,
                                  price_per_call: response.data!.price_per_call,
                                  pulse_seconds: response.data!.pulse_seconds,
                              }
                            : wf,
                    ),
                );
                toast.success("Agent billing updated");
            }
        } finally {
            setSavingWorkflowId(null);
        }
    };

    if (isLoading) {
        return (
            <main className="container mx-auto p-6 max-w-5xl">
                <div className="flex items-center justify-center py-24 text-muted-foreground">
                    <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                    Loading wallet...
                </div>
            </main>
        );
    }

    if (error || !wallet) {
        return (
            <main className="container mx-auto p-6 max-w-5xl">
                <div className="flex items-center gap-2 mb-4">
                    <Link href="/superadmin/wallets">
                        <Button variant="ghost" size="sm">
                            <ArrowLeft className="h-4 w-4" />
                        </Button>
                    </Link>
                </div>
                <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                    {error || "Wallet not found"}
                </div>
            </main>
        );
    }

    const availableBalance = Number(wallet.wallet_balance) - Number(wallet.credit_limit);
    const totalPages = Math.max(1, Math.ceil(totalTransactions / TRANSACTIONS_PAGE_SIZE));

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-5xl">
            <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                    <Link href="/superadmin/wallets">
                        <Button variant="ghost" size="sm">
                            <ArrowLeft className="h-4 w-4" />
                        </Button>
                    </Link>
                    <div>
                        <h1 className="text-3xl font-bold">
                            {organization?.name || `Organization #${organizationId}`}
                        </h1>
                        <p className="text-sm text-muted-foreground mt-1">Wallet management</p>
                    </div>
                </div>
                <div className="flex items-center gap-3 rounded-lg border px-4 py-2">
                    <div className="flex flex-col">
                        <span className="text-sm font-medium">
                            Wallet {wallet.wallet_enabled ? "Enabled" : "Disabled"}
                        </span>
                        <span className="text-xs text-muted-foreground">
                            {wallet.wallet_enabled
                                ? "Visible to the client, billing active"
                                : "Hidden from the client, billing off"}
                        </span>
                    </div>
                    <Switch
                        checked={wallet.wallet_enabled}
                        disabled={isTogglingWallet}
                        onCheckedChange={handleToggleWalletEnabled}
                    />
                </div>
            </div>

            <div className="grid gap-6 md:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle>Balance</CardTitle>
                        <CardDescription>Prepaid balance and credit floor</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <div>
                            <div className="text-3xl font-bold">
                                {formatMoney(wallet.wallet_balance, wallet.wallet_currency)}
                            </div>
                            <div className="text-sm text-muted-foreground mt-1">
                                Available to spend: {formatMoney(availableBalance, wallet.wallet_currency)}
                                {Number(wallet.credit_limit) > 0 && (
                                    <> (includes {formatMoney(wallet.credit_limit, wallet.wallet_currency)} credit)</>
                                )}
                            </div>
                        </div>
                        <div className="flex gap-2">
                            <Button size="sm" onClick={() => setTopupOpen(true)}>
                                <Plus className="mr-2 h-4 w-4" />
                                Top Up
                            </Button>
                            <Button size="sm" variant="outline" onClick={() => setAdjustOpen(true)}>
                                <Minus className="mr-2 h-4 w-4" />
                                Adjust
                            </Button>
                        </div>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle>Billing Settings</CardTitle>
                        <CardDescription>
                            Currency and credit floor for this org. The rate is set per agent below.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-3">
                        <div className="grid grid-cols-2 gap-3">
                            <div className="space-y-1">
                                <Label htmlFor="currency">Currency</Label>
                                <Select value={currency} onValueChange={setCurrency}>
                                    <SelectTrigger id="currency" className="w-full">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="INR">INR</SelectItem>
                                        <SelectItem value="USD">USD</SelectItem>
                                    </SelectContent>
                                </Select>
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="credit-limit">Credit Limit</Label>
                                <Input
                                    id="credit-limit"
                                    value={creditLimit}
                                    onChange={(e) => setCreditLimit(e.target.value)}
                                    placeholder="0"
                                />
                            </div>
                        </div>
                        <Button size="sm" onClick={handleSaveSettings} disabled={isSavingSettings}>
                            {isSavingSettings ? (
                                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            ) : (
                                <Save className="mr-2 h-4 w-4" />
                            )}
                            Save Settings
                        </Button>
                    </CardContent>
                </Card>
            </div>

            <Card>
                <CardHeader>
                    <CardTitle>Agents — Billing</CardTitle>
                    <CardDescription>
                        Each agent bills either per minute or per call — exactly one at a time.
                        Per-minute calls round up to the pulse (any part of a pulse is a full
                        pulse; pay as you go bills exact seconds). Per-minute reservations size
                        off rows × avg minutes × rate; per-call reservations size off rows × rate.
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    {workflows.length === 0 ? (
                        <div className="py-6 text-center text-sm text-muted-foreground">
                            No agents for this organization yet.
                        </div>
                    ) : (
                        <div className="overflow-x-auto">
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>Agent</TableHead>
                                        <TableHead>Pricing Mode</TableHead>
                                        <TableHead>Rate</TableHead>
                                        <TableHead className="text-right">Actions</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {workflows.map((wf) => {
                                        const mode = modeDrafts[wf.id] ?? "per_minute";
                                        return (
                                            <TableRow key={wf.id}>
                                                <TableCell className="font-medium">{wf.name}</TableCell>
                                                <TableCell>
                                                    <Select
                                                        value={mode}
                                                        onValueChange={(value) =>
                                                            setModeDrafts((prev) => ({
                                                                ...prev,
                                                                [wf.id]: value,
                                                            }))
                                                        }
                                                    >
                                                        <SelectTrigger className="w-36 bg-muted">
                                                            <SelectValue />
                                                        </SelectTrigger>
                                                        <SelectContent>
                                                            <SelectItem value="per_minute">Per Minute</SelectItem>
                                                            <SelectItem value="per_call">Per Call</SelectItem>
                                                        </SelectContent>
                                                    </Select>
                                                </TableCell>
                                                <TableCell>
                                                    {mode === "per_call" ? (
                                                        <div className="space-y-1">
                                                            <Label
                                                                htmlFor={`price-call-${wf.id}`}
                                                                className="text-xs font-normal text-muted-foreground"
                                                            >
                                                                Price / call
                                                            </Label>
                                                            <Input
                                                                id={`price-call-${wf.id}`}
                                                                className="w-28"
                                                                value={perCallRateDrafts[wf.id] ?? ""}
                                                                onChange={(e) =>
                                                                    setPerCallRateDrafts((prev) => ({
                                                                        ...prev,
                                                                        [wf.id]: e.target.value,
                                                                    }))
                                                                }
                                                                placeholder="Not set"
                                                            />
                                                        </div>
                                                    ) : (
                                                        <div className="flex gap-2">
                                                            <div className="space-y-1">
                                                                <Label
                                                                    htmlFor={`price-min-${wf.id}`}
                                                                    className="text-xs font-normal text-muted-foreground"
                                                                >
                                                                    Price / min
                                                                </Label>
                                                                <Input
                                                                    id={`price-min-${wf.id}`}
                                                                    className="w-28"
                                                                    value={rateDrafts[wf.id] ?? ""}
                                                                    onChange={(e) =>
                                                                        setRateDrafts((prev) => ({
                                                                            ...prev,
                                                                            [wf.id]: e.target.value,
                                                                        }))
                                                                    }
                                                                    placeholder="Not set"
                                                                />
                                                            </div>
                                                            <div className="space-y-1">
                                                                <Label
                                                                    htmlFor={`pulse-${wf.id}`}
                                                                    className="text-xs font-normal text-muted-foreground"
                                                                >
                                                                    Pulse
                                                                </Label>
                                                                <Select
                                                                    value={pulseDrafts[wf.id] ?? "0"}
                                                                    onValueChange={(value) =>
                                                                        setPulseDrafts((prev) => ({
                                                                            ...prev,
                                                                            [wf.id]: value,
                                                                        }))
                                                                    }
                                                                >
                                                                    <SelectTrigger id={`pulse-${wf.id}`} className="w-36">
                                                                        <SelectValue />
                                                                    </SelectTrigger>
                                                                    <SelectContent>
                                                                        {PULSE_OPTIONS.map((option) => (
                                                                            <SelectItem key={option.value} value={option.value}>
                                                                                {option.label}
                                                                            </SelectItem>
                                                                        ))}
                                                                    </SelectContent>
                                                                </Select>
                                                            </div>
                                                            <div className="space-y-1">
                                                                <Label
                                                                    htmlFor={`avg-min-${wf.id}`}
                                                                    className="text-xs font-normal text-muted-foreground"
                                                                >
                                                                    Avg. min/call
                                                                </Label>
                                                                <Input
                                                                    id={`avg-min-${wf.id}`}
                                                                    className="w-28"
                                                                    value={durationDrafts[wf.id] ?? ""}
                                                                    onChange={(e) =>
                                                                        setDurationDrafts((prev) => ({
                                                                            ...prev,
                                                                            [wf.id]: e.target.value,
                                                                        }))
                                                                    }
                                                                    placeholder="e.g. 3.5"
                                                                />
                                                            </div>
                                                        </div>
                                                    )}
                                                </TableCell>
                                                <TableCell className="text-right">
                                                    <Button
                                                        size="sm"
                                                        variant="outline"
                                                        disabled={savingWorkflowId === wf.id}
                                                        onClick={() => handleSaveWorkflowBilling(wf.id)}
                                                    >
                                                        {savingWorkflowId === wf.id ? (
                                                            <Loader2 className="h-4 w-4 animate-spin" />
                                                        ) : (
                                                            "Save"
                                                        )}
                                                    </Button>
                                                </TableCell>
                                            </TableRow>
                                        );
                                    })}
                                </TableBody>
                            </Table>
                        </div>
                    )}
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Ledger</CardTitle>
                    <CardDescription>{totalTransactions} transactions</CardDescription>
                </CardHeader>
                <CardContent>
                    {transactions.length === 0 ? (
                        <div className="py-6 text-center text-sm text-muted-foreground">
                            No transactions yet.
                        </div>
                    ) : (
                        <>
                            <div className="bg-card border rounded-lg overflow-x-auto shadow-sm">
                                <Table>
                                    <TableHeader>
                                        <TableRow className="bg-muted/50">
                                            <TableHead>Date</TableHead>
                                            <TableHead>Activity</TableHead>
                                            <TableHead>Reference</TableHead>
                                            <TableHead className="text-right">Amount</TableHead>
                                            <TableHead className="text-right">Balance</TableHead>
                                        </TableRow>
                                    </TableHeader>
                                    <TableBody>
                                        {transactions.map((tx) => {
                                            const reference = tx.workflow_run_id
                                                ? `#${tx.workflow_run_id}`
                                                : tx.campaign_id
                                                  ? `Campaign #${tx.campaign_id}`
                                                  : null;
                                            return (
                                                <TableRow key={tx.id}>
                                                    <TableCell className="whitespace-nowrap text-muted-foreground">
                                                        {formatDate(tx.created_at)}
                                                    </TableCell>
                                                    <TableCell>
                                                        <div className="flex flex-col gap-1">
                                                            <span className="font-medium">
                                                                {TX_TYPE_LABEL[tx.type] ?? tx.type}
                                                            </span>
                                                            {tx.note && (
                                                                <span className="text-xs text-muted-foreground">
                                                                    {tx.note}
                                                                </span>
                                                            )}
                                                        </div>
                                                    </TableCell>
                                                    <TableCell>{reference ?? "-"}</TableCell>
                                                    <TableCell className="text-right">
                                                        <div
                                                            className={`font-medium ${
                                                                Number(tx.amount) < 0
                                                                    ? "text-destructive"
                                                                    : "text-green-600"
                                                            }`}
                                                        >
                                                            {Number(tx.amount) >= 0 ? "+" : ""}
                                                            {formatMoney(tx.amount, tx.currency)}
                                                        </div>
                                                    </TableCell>
                                                    <TableCell className="text-right">
                                                        {tx.balance_after !== null
                                                            ? formatMoney(tx.balance_after, tx.currency)
                                                            : "-"}
                                                    </TableCell>
                                                </TableRow>
                                            );
                                        })}
                                    </TableBody>
                                </Table>
                            </div>
                            <div className="flex items-center justify-between mt-4">
                                <Button
                                    variant="outline"
                                    size="sm"
                                    disabled={page <= 1}
                                    onClick={() => fetchTransactions(page - 1)}
                                >
                                    Previous
                                </Button>
                                <span className="text-sm text-muted-foreground">
                                    Page {page} of {totalPages}
                                </span>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    disabled={page >= totalPages}
                                    onClick={() => fetchTransactions(page + 1)}
                                >
                                    Next
                                </Button>
                            </div>
                        </>
                    )}
                </CardContent>
            </Card>

            <Dialog open={topupOpen} onOpenChange={(open) => !isTopupSubmitting && setTopupOpen(open)}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>Top Up Wallet</DialogTitle>
                        <DialogDescription>Add funds to this organization&apos;s wallet.</DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4 py-2">
                        <div className="space-y-2">
                            <Label htmlFor="topup-amount">Amount</Label>
                            <Input
                                id="topup-amount"
                                value={topupAmount}
                                onChange={(e) => setTopupAmount(e.target.value)}
                                placeholder="1000"
                                disabled={isTopupSubmitting}
                            />
                        </div>
                        <div className="space-y-2">
                            <Label htmlFor="topup-note">Note (optional)</Label>
                            <Input
                                id="topup-note"
                                value={topupNote}
                                onChange={(e) => setTopupNote(e.target.value)}
                                placeholder="e.g. Invoice #1234"
                                disabled={isTopupSubmitting}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setTopupOpen(false)} disabled={isTopupSubmitting}>
                            Cancel
                        </Button>
                        <Button onClick={handleTopup} disabled={isTopupSubmitting || !topupAmount.trim()}>
                            {isTopupSubmitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                            Top Up
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={adjustOpen} onOpenChange={(open) => !isAdjustSubmitting && setAdjustOpen(open)}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>Manual Adjustment</DialogTitle>
                        <DialogDescription>
                            Correct the balance directly. Use a negative amount to deduct.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4 py-2">
                        <div className="space-y-2">
                            <Label htmlFor="adjust-amount">Amount (use - to deduct)</Label>
                            <Input
                                id="adjust-amount"
                                value={adjustAmount}
                                onChange={(e) => setAdjustAmount(e.target.value)}
                                placeholder="-50"
                                disabled={isAdjustSubmitting}
                            />
                        </div>
                        <div className="space-y-2">
                            <Label htmlFor="adjust-note">Reason (required)</Label>
                            <Input
                                id="adjust-note"
                                value={adjustNote}
                                onChange={(e) => setAdjustNote(e.target.value)}
                                placeholder="e.g. Goodwill credit for outage"
                                disabled={isAdjustSubmitting}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setAdjustOpen(false)} disabled={isAdjustSubmitting}>
                            Cancel
                        </Button>
                        <Button
                            onClick={handleAdjust}
                            disabled={isAdjustSubmitting || !adjustAmount.trim() || !adjustNote.trim()}
                        >
                            {isAdjustSubmitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                            Adjust
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </main>
    );
}
