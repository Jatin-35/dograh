'use client';

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import type { StatsHour } from '@/client/types.gen';

import { formatCount, hourLabel } from './format';

const TICKS = [0, 3, 6, 9, 12, 15, 18, 21];

function HourlyTooltip({ active, payload }: { active?: boolean; payload?: ReadonlyArray<{ payload?: StatsHour }> }) {
    const point = payload?.[0]?.payload;
    if (!active || !point) return null;
    return (
        <div className="rounded-lg border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md">
            <p className="mb-1 font-medium">
                {hourLabel(point.hour)} – {String(point.hour).padStart(2, '0')}:59
            </p>
            <p className="flex items-center justify-between gap-6">
                <span className="text-muted-foreground">Calls</span>
                <span className="tabular-nums">{formatCount(point.calls)}</span>
            </p>
        </div>
    );
}

// One series, so one colour and no legend: the card title names what it counts.
export function HourlyCallsChart({ hours }: { hours: StatsHour[] }) {
    return (
        <ResponsiveContainer width="100%" height={260}>
            <BarChart data={hours} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barCategoryGap="20%">
                <CartesianGrid vertical={false} stroke="var(--cd-grid)" />
                <XAxis
                    dataKey="hour"
                    ticks={TICKS}
                    tickFormatter={hourLabel}
                    tickLine={false}
                    axisLine={{ stroke: 'var(--cd-baseline)' }}
                    tick={{ fill: 'var(--muted-foreground)', fontSize: 12 }}
                />
                <YAxis
                    allowDecimals={false}
                    tickLine={false}
                    axisLine={false}
                    width={32}
                    tick={{ fill: 'var(--muted-foreground)', fontSize: 12 }}
                />
                <Tooltip cursor={{ fill: 'var(--cd-hover)' }} content={<HourlyTooltip />} />
                <Bar isAnimationActive={false} dataKey="calls" fill="var(--cd-series)" radius={[4, 4, 0, 0]} maxBarSize={16} />
            </BarChart>
        </ResponsiveContainer>
    );
}
