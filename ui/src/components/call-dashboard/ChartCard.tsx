'use client';

import { ReactNode, useState } from 'react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';

export interface ChartTable {
    columns: string[];
    rows: Array<Array<string | number>>;
}

interface ChartCardProps {
    title: string;
    caption?: string;
    // The same numbers as the chart, as a table: the accessible twin of every chart.
    table: ChartTable;
    className?: string;
    children: ReactNode;
}

function DataTable({ table }: { table: ChartTable }) {
    return (
        <div className="overflow-x-auto">
            <table className="w-full text-sm">
                <thead>
                    <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                        {table.columns.map((column, index) => (
                            <th key={column} className={cn('py-2 font-medium', index > 0 && 'text-right')}>
                                {column}
                            </th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {table.rows.map((row, rowIndex) => (
                        <tr key={rowIndex} className="border-b border-border/60 last:border-0">
                            {row.map((cell, index) => (
                                <td
                                    key={index}
                                    className={cn('py-2', index > 0 && 'text-right tabular-nums text-muted-foreground')}
                                >
                                    {cell}
                                </td>
                            ))}
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

export function ChartCard({ title, caption, table, className, children }: ChartCardProps) {
    const [view, setView] = useState<'chart' | 'table'>('chart');

    return (
        <Card className={className}>
            <CardHeader className="flex-row items-start justify-between gap-3 space-y-0 p-5 pb-4">
                <div className="min-w-0 space-y-1">
                    <CardTitle className="text-base">{title}</CardTitle>
                    {caption && <CardDescription className="text-xs">{caption}</CardDescription>}
                </div>
                <div
                    role="group"
                    aria-label={`${title} view`}
                    className="inline-flex shrink-0 rounded-md border border-border p-0.5 text-xs"
                >
                    {(['chart', 'table'] as const).map((option) => (
                        <button
                            key={option}
                            type="button"
                            aria-pressed={view === option}
                            onClick={() => setView(option)}
                            className={cn(
                                'rounded px-2 py-1 capitalize transition-colors',
                                view === option
                                    ? 'bg-muted text-foreground'
                                    : 'text-muted-foreground hover:text-foreground',
                            )}
                        >
                            {option}
                        </button>
                    ))}
                </div>
            </CardHeader>
            <CardContent className="p-5 pt-0">{view === 'chart' ? children : <DataTable table={table} />}</CardContent>
        </Card>
    );
}
