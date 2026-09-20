"use client";

import { Wallet } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { getMyWalletApiV1WalletMeGet } from "@/client/sdk.gen";
import type { WalletSettingsResponse } from "@/client/types.gen";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth";

export function formatMoney(amount: string | number, currency: string): string {
    const value = Number(amount);
    if (Number.isNaN(value)) return String(amount);
    try {
        return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(value);
    } catch {
        return `${value.toFixed(2)} ${currency}`;
    }
}

/**
 * Compact wallet balance summary for the client Overview page. Renders
 * nothing unless a superadmin has switched wallet billing on for this org
 * — most orgs on hosted/hybrid billing never see this card.
 */
export function WalletBalanceCard({ linkHref }: { linkHref?: string }) {
    const { user, loading: authLoading } = useAuth();
    const [wallet, setWallet] = useState<WalletSettingsResponse | null>(null);
    const hasFetched = useRef(false);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        (async () => {
            const response = await getMyWalletApiV1WalletMeGet();
            if (response.data && response.data.wallet_enabled) {
                setWallet(response.data);
            }
        })();
    }, [authLoading, user]);

    if (!wallet) return null;

    const available = Number(wallet.wallet_balance) - Number(wallet.credit_limit);
    const isLow = available <= 0;

    const card = (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <Wallet className="h-5 w-5 text-muted-foreground" />
                    Wallet Balance
                </CardTitle>
                <CardDescription>
                    {isLow
                        ? "Balance is low — top up to keep making calls"
                        : "Your current prepaid balance"}
                </CardDescription>
            </CardHeader>
            <CardContent>
                <div className={`text-2xl font-bold ${isLow ? "text-red-600" : ""}`}>
                    {formatMoney(wallet.wallet_balance, wallet.wallet_currency)}
                </div>
            </CardContent>
        </Card>
    );

    return linkHref ? (
        <Link href={linkHref} className="block">
            {card}
        </Link>
    ) : (
        card
    );
}
