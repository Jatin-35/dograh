"use client";

import { ChevronLeft, ChevronRight, Wallet } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
    getMyWalletApiV1WalletMeGet,
    listMyWalletTransactionsApiV1WalletMeTransactionsGet,
} from "@/client/sdk.gen";
import type { WalletSettingsResponse, WalletTransactionResponse } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { useAuth } from "@/lib/auth";

import { formatMoney } from "./WalletBalanceCard";

const PAGE_SIZE = 20;

const TX_TYPE_LABEL: Record<string, string> = {
    topup: "Top-up",
    debit: "Call charge",
    adjustment: "Manual adjustment",
    refund: "Refund",
    campaign_reserve: "Campaign reserved",
    campaign_cost: "Campaign call cost",
    campaign_reconcile: "Campaign reconciled",
};

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

/**
 * Wallet balance + statement, shown on the Billing page only for orgs
 * where a superadmin has switched wallet billing on. Renders nothing
 * otherwise — most hosted/MPS-billed orgs never see this.
 */
export function WalletLedgerSection() {
    const { user, loading: authLoading } = useAuth();
    const [wallet, setWallet] = useState<WalletSettingsResponse | null>(null);
    const [transactions, setTransactions] = useState<WalletTransactionResponse[]>([]);
    const [totalCount, setTotalCount] = useState(0);
    const [page, setPage] = useState(1);
    const [isLoading, setIsLoading] = useState(true);
    const hasFetched = useRef(false);

    const fetchTransactions = useCallback(async (targetPage: number) => {
        const response = await listMyWalletTransactionsApiV1WalletMeTransactionsGet({
            query: { page: targetPage, limit: PAGE_SIZE },
        });
        if (response.data) {
            setTransactions(response.data.transactions);
            setTotalCount(response.data.total_count);
            setPage(targetPage);
        }
    }, []);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        (async () => {
            const walletRes = await getMyWalletApiV1WalletMeGet();
            if (walletRes.data && walletRes.data.wallet_enabled) {
                setWallet(walletRes.data);
                await fetchTransactions(1);
            }
            setIsLoading(false);
        })();
    }, [authLoading, user, fetchTransactions]);

    if (isLoading || !wallet) return null;

    const available = Number(wallet.wallet_balance) - Number(wallet.credit_limit);
    const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <Wallet className="h-5 w-5 text-muted-foreground" />
                    Wallet
                </CardTitle>
                <CardDescription>Prepaid balance and call charge statement</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
                <div className="grid gap-4 md:grid-cols-2">
                    <div>
                        <div className="text-sm text-muted-foreground">Balance</div>
                        <div className="text-3xl font-bold">
                            {formatMoney(wallet.wallet_balance, wallet.wallet_currency)}
                        </div>
                    </div>
                    <div>
                        <div className="text-sm text-muted-foreground">Available to spend</div>
                        <div className="text-3xl font-bold">
                            {formatMoney(available, wallet.wallet_currency)}
                        </div>
                    </div>
                </div>

                {transactions.length > 0 ? (
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
                                    const runHref =
                                        tx.workflow_id && tx.workflow_run_id
                                            ? `/workflow/${tx.workflow_id}/run/${tx.workflow_run_id}`
                                            : null;
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
                                            <TableCell>
                                                {runHref ? (
                                                    <Link
                                                        className="font-medium text-primary hover:underline"
                                                        href={runHref}
                                                    >
                                                        {reference}
                                                    </Link>
                                                ) : (
                                                    (reference ?? "-")
                                                )}
                                            </TableCell>
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
                ) : (
                    <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground">
                        No transactions yet
                    </div>
                )}

                {totalPages > 1 && (
                    <div className="flex items-center justify-between">
                        <p className="text-sm text-muted-foreground">
                            Page {page} of {totalPages} ({totalCount} total)
                        </p>
                        <div className="flex gap-2">
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => fetchTransactions(page - 1)}
                                disabled={page <= 1}
                            >
                                <ChevronLeft className="h-4 w-4" />
                                Previous
                            </Button>
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => fetchTransactions(page + 1)}
                                disabled={page >= totalPages}
                            >
                                Next
                                <ChevronRight className="h-4 w-4" />
                            </Button>
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
