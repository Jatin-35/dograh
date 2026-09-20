import { Bot, ShieldCheck, SlidersHorizontal } from 'lucide-react';
import Link from 'next/link';

import { Button } from '@/components/ui/button';

interface QuickActionsProps {
    // Only superadmins get the Super Admin shortcut.
    showSuperAdmin: boolean;
}

/**
 * Shortcuts for the people who build and configure agents (the superadmin and
 * impersonation domains). Clients never see this: it is not part of their Home.
 */
export function QuickActions({ showSuperAdmin }: QuickActionsProps) {
    return (
        <div className="flex flex-wrap items-center gap-2" aria-label="Quick actions" role="group">
            <span className="mr-1 text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
                Quick actions
            </span>
            <Button asChild size="sm" variant="outline" className="gap-2">
                <Link href="/workflow">
                    <Bot className="h-4 w-4" aria-hidden="true" />
                    Agents
                </Link>
            </Button>
            <Button asChild size="sm" variant="outline" className="gap-2">
                <Link href="/model-configurations">
                    <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
                    Configure models
                </Link>
            </Button>
            {showSuperAdmin && (
                <Button asChild size="sm" variant="outline" className="gap-2">
                    <Link href="/superadmin">
                        <ShieldCheck className="h-4 w-4" aria-hidden="true" />
                        Super Admin
                    </Link>
                </Button>
            )}
        </div>
    );
}
