"use client";

import { Wallet } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { getMyWalletApiV1WalletMeGet } from "@/client/sdk.gen";
import type { WalletSettingsResponse } from "@/client/types.gen";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useAuth } from "@/lib/auth";

import { formatMoney } from "./WalletBalanceCard";

/**
 * Compact wallet balance pill for the global header, next to the
 * Join WhatsApp/Subscribe badges. Renders nothing unless a superadmin has
 * switched wallet billing on for the currently selected org.
 */
export function WalletHeaderBadge() {
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

    return (
        <TooltipProvider>
            <Tooltip>
                <TooltipTrigger asChild>
                    <Link
                        href="/billing"
                        className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm font-medium leading-none transition-colors ${
                            isLow
                                ? "border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/15"
                                : "bg-muted hover:bg-muted/70"
                        }`}
                    >
                        <Wallet className="h-4 w-4" />
                        <span>{formatMoney(wallet.wallet_balance, wallet.wallet_currency)}</span>
                    </Link>
                </TooltipTrigger>
                <TooltipContent>{isLow ? "Balance low — top up" : "Wallet balance"}</TooltipContent>
            </Tooltip>
        </TooltipProvider>
    );
}
