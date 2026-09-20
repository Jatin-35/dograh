'use client';

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import type { StatsDay } from '@/client/types.gen';

import { formatCount, longDate, shortDate } from './format';

interface Point {
    date: string;
    label: string;
    successful: number;
    other: number;
    calls: number;
}

interface ShapeProps {
    x?: number;
    y?: number;
    width?: number;
    height?: number;
    fill?: string;
    payload?: Point;
}

// A stacked column with only its *top* end rounded (4px, anchored to the
// baseline). Which segment is on top depends on the day, so it is decided per
// point: the "other" segment is always above "successful", unless it is empty.
function stackShape(isTop: (point: Point) => boolean) {
    return function StackSegment({ x = 0, y = 0, width = 0, height = 0, fill, payload }: ShapeProps) {
        if (width <= 0 || height <= 0) return null;
        const radius = payload && isTop(payload) ? Math.min(4, width / 2, height) : 0;
        const path = [
            `M${x},${y + height}`,
            `L${x},${y + radius}`,
            `Q${x},${y} ${x + radius},${y}`,
            `L${x + width - radius},${y}`,
            `Q${x + width},${y} ${x + width},${y + radius}`,
            `L${x + width},${y + height}`,
            'Z',
        ].join(' ');
        // The 2px stroke in the card colour is the gap between stacked segments.
        return <path d={path} fill={fill} stroke="var(--card)" strokeWidth={2} />;
    };
}

const SuccessfulSegment = stackShape((point) => point.other === 0);
const OtherSegment = stackShape(() => true);

function DailyTooltip({ active, payload }: { active?: boolean; payload?: ReadonlyArray<{ payload?: Point }> }) {
    const point = payload?.[0]?.payload;
    if (!active || !point) return null;
    return (
        <div className="rounded-lg border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md">
            <p className="mb-1 font-medium">{longDate(point.date)}</p>
            <p className="flex items-center justify-between gap-6">
                <span className="text-muted-foreground">Successful</span>
                <span className="tabular-nums">{formatCount(point.successful)}</span>
            </p>
            <p className="flex items-center justify-between gap-6">
                <span className="text-muted-foreground">Not successful</span>
                <span className="tabular-nums">{formatCount(point.other)}</span>
            </p>
            <p className="mt-1 flex items-center justify-between gap-6 border-t border-border pt-1 font-medium">
                <span>Total</span>
                <span className="tabular-nums">{formatCount(point.calls)}</span>
            </p>
        </div>
    );
}

function LegendItem({ color, label }: { color: string; label: string }) {
    return (
        <span className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} aria-hidden="true" />
            {label}
        </span>
    );
}

export function DailyCallsChart({ days }: { days: StatsDay[] }) {
    const data: Point[] = days.map((day) => ({
        date: day.date,
        label: shortDate(day.date),
        successful: day.successful,
        other: day.calls - day.successful,
        calls: day.calls,
    }));

    return (
        <div>
            <div className="mb-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
                <LegendItem color="var(--cd-series)" label="Successful" />
                <LegendItem color="var(--cd-neutral)" label="Not successful" />
            </div>
            <ResponsiveContainer width="100%" height={260}>
                <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barCategoryGap="28%">
                    <CartesianGrid vertical={false} stroke="var(--cd-grid)" />
                    <XAxis
                        dataKey="label"
                        tickLine={false}
                        axisLine={{ stroke: 'var(--cd-baseline)' }}
                        tick={{ fill: 'var(--muted-foreground)', fontSize: 12 }}
                        interval="preserveStartEnd"
                        minTickGap={20}
                    />
                    <YAxis
                        allowDecimals={false}
                        tickLine={false}
                        axisLine={false}
                        width={32}
                        tick={{ fill: 'var(--muted-foreground)', fontSize: 12 }}
                    />
                    <Tooltip cursor={{ fill: 'var(--cd-hover)' }} content={<DailyTooltip />} />
                    <Bar
                        isAnimationActive={false}
                        dataKey="successful"
                        stackId="calls"
                        fill="var(--cd-series)"
                        maxBarSize={28}
                        shape={(props: unknown) => <SuccessfulSegment {...(props as ShapeProps)} />}
                    />
                    <Bar
                        isAnimationActive={false}
                        dataKey="other"
                        stackId="calls"
                        fill="var(--cd-neutral)"
                        maxBarSize={28}
                        shape={(props: unknown) => <OtherSegment {...(props as ShapeProps)} />}
                    />
                </BarChart>
            </ResponsiveContainer>
        </div>
    );
}
