import type { LucideIcon } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';

interface StatTileProps {
    label: string;
    value: string;
    sub?: string;
    icon: LucideIcon;
    // Longer explanation, shown on hover of the value (what the number counts).
    hint?: string;
}

// A headline number is the chart: no plot, just the figure and what it counts.
export function StatTile({ label, value, sub, icon: Icon, hint }: StatTileProps) {
    return (
        <Card>
            <CardContent className="space-y-1 p-5">
                <div className="flex items-center justify-between gap-2">
                    <p className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
                    <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                </div>
                <p className="text-3xl font-semibold text-foreground" title={hint}>
                    {value}
                </p>
                {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
            </CardContent>
        </Card>
    );
}
