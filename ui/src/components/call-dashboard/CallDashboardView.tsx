'use client';

import './call-dashboard.css';

import { AlertTriangle, Clock, Phone, ShieldCheck, Ticket } from 'lucide-react';
import { ReactNode } from 'react';

import type { CallStatsResponse, StatsItem } from '@/client/types.gen';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

import { BarList } from './BarList';
import { ChartCard, ChartTable } from './ChartCard';
import { DailyCallsChart } from './DailyCallsChart';
import { DivergingBar, DivergingSegment } from './DivergingBar';
import { formatCount, formatDuration, formatPercent, hourLabel, shareOf, shortDate } from './format';
import { HourlyCallsChart } from './HourlyCallsChart';
import { StatTile } from './StatTile';

interface CallDashboardViewProps {
    stats: CallStatsResponse | null;
    // A refetch is in flight: keep showing the last numbers, dimmed.
    refreshing?: boolean;
    error?: string | null;
}

function countOf(items: StatsItem[], key: string): number {
    return items.find((item) => item.key === key)?.count ?? 0;
}

function shareTable(items: StatsItem[], columns: [string, string, string]): ChartTable {
    const total = items.reduce((sum, item) => sum + item.count, 0);
    return {
        columns,
        rows: items.length
            ? items.map((item) => [item.label, formatCount(item.count), shareOf(item.count, total)])
            : [['No data', '0', '0%']],
    };
}

export function CallDashboardView({ stats, refreshing = false, error = null }: CallDashboardViewProps) {
    if (!stats) {
        return error ? (
            <p role="alert" className="text-sm text-destructive">
                {error}
            </p>
        ) : null;
    }

    const { kpis } = stats;
    const total = kpis.total_calls;

    // What the agents in scope can ever produce. A tile or chart that could never
    // have data (tickets for a sales agent, sentiment where there is no QA) is left
    // out rather than shown empty. The API always sends this; a response without it (a
    // newer UI talking to an older server during a deploy) shows everything.
    const hasTickets = stats.capabilities?.tickets ?? true;
    const hasAnalysis = stats.capabilities?.analysis ?? true;
    const hasOutcomes = hasTickets || hasAnalysis;

    const analysedCaption =
        kpis.analysed_calls === 0
            ? 'No calls were analysed in this period'
            : `Based on ${formatCount(kpis.analysed_calls)} of ${formatCount(total)} calls analysed`;

    const sentimentSegments: DivergingSegment[] = [
        { key: 'negative', label: 'Negative', count: countOf(stats.sentiment, 'negative'), tone: 'negative' },
        { key: 'neutral', label: 'Neutral', count: countOf(stats.sentiment, 'neutral'), tone: 'neutral' },
        { key: 'positive', label: 'Positive', count: countOf(stats.sentiment, 'positive'), tone: 'positive' },
    ];
    const satisfactionSegments: DivergingSegment[] = [
        { key: 'no', label: 'Not satisfied', count: countOf(stats.satisfaction, 'no'), tone: 'negative' },
        { key: 'unclear', label: 'Unclear', count: countOf(stats.satisfaction, 'unclear'), tone: 'neutral' },
        { key: 'yes', label: 'Satisfied', count: countOf(stats.satisfaction, 'yes'), tone: 'positive' },
    ];
    const segmentTable = (segments: DivergingSegment[]): ChartTable => {
        const sum = segments.reduce((acc, segment) => acc + segment.count, 0);
        return {
            columns: ['Answer', 'Calls', 'Share'],
            rows: segments.map((s) => [s.label, formatCount(s.count), shareOf(s.count, sum)]),
        };
    };

    const busyHours = stats.hourly.filter((hour) => hour.calls > 0);

    const breakdownCards: ReactNode[] = [];
    if (hasOutcomes) {
        breakdownCards.push(
            <ChartCard
                key="outcomes"
                title="Call outcomes"
                table={shareTable(stats.outcomes, ['Outcome', 'Calls', 'Share'])}
            >
                <BarList items={stats.outcomes} emptyLabel="No calls in this period" />
            </ChartCard>,
        );
    }
    if (hasAnalysis) {
        breakdownCards.push(
            <ChartCard
                key="reasons"
                title="Reason for call"
                caption={analysedCaption}
                table={shareTable(stats.reasons, ['Reason', 'Calls', 'Share'])}
            >
                <BarList items={stats.reasons} emptyLabel="No analysed calls in this period" />
            </ChartCard>,
            <ChartCard
                key="sentiment"
                title="Customer sentiment"
                caption={analysedCaption}
                table={segmentTable(sentimentSegments)}
            >
                <DivergingBar segments={sentimentSegments} emptyLabel="No analysed calls in this period" />
            </ChartCard>,
            <ChartCard
                key="satisfaction"
                title="Customer satisfied"
                caption={analysedCaption}
                table={segmentTable(satisfactionSegments)}
            >
                <DivergingBar segments={satisfactionSegments} emptyLabel="No analysed calls in this period" />
            </ChartCard>,
        );
    }
    breakdownCards.push(
        <ChartCard
            key="endings"
            title="How calls ended"
            table={shareTable(stats.disconnections, ['Ending', 'Calls', 'Share'])}
        >
            <BarList items={stats.disconnections} emptyLabel="No calls in this period" />
        </ChartCard>,
    );

    return (
        <div className={cn('cd-root space-y-6 transition-opacity', refreshing && 'opacity-60')} aria-busy={refreshing}>
            {error && (
                <p role="alert" className="text-sm text-destructive">
                    {error}
                </p>
            )}

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-[repeat(auto-fit,minmax(200px,1fr))]">
                <StatTile
                    label="Total calls"
                    value={formatCount(total)}
                    sub={`${formatCount(kpis.inbound_calls)} inbound · ${formatCount(kpis.outbound_calls)} outbound`}
                    icon={Phone}
                />
                <StatTile
                    label="Success rate"
                    value={formatPercent(kpis.success_rate)}
                    sub={`${formatCount(kpis.successful_calls)} of ${formatCount(total)} calls completed`}
                    icon={ShieldCheck}
                    hint="A completed call finished normally, without a system failure, and lasted at least 10 seconds."
                />
                <StatTile
                    label="Average duration"
                    value={formatDuration(kpis.avg_duration_seconds)}
                    sub={`${formatDuration(kpis.total_duration_seconds)} total talk time`}
                    icon={Clock}
                />
                {hasTickets && (
                    <StatTile
                        label="Tickets created"
                        value={formatCount(kpis.tickets_created)}
                        sub={`${formatCount(kpis.calls_with_closed_ticket)} calls closed · ${formatCount(kpis.calls_with_open_ticket)} still open`}
                        icon={Ticket}
                    />
                )}
                <StatTile
                    label="Failed calls"
                    value={formatCount(kpis.failed_calls)}
                    sub="System errors and calls that did not connect"
                    icon={AlertTriangle}
                />
            </div>

            {total === 0 ? (
                <Card>
                    <CardContent className="py-12 text-center">
                        <p className="text-base font-medium text-foreground">No calls in this period</p>
                        <p className="mt-1 text-sm text-muted-foreground">
                            Try a longer date range, or a different agent or call type.
                        </p>
                    </CardContent>
                </Card>
            ) : (
                <>
                    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                        <ChartCard
                            title="Calls per day"
                            className="lg:col-span-2"
                            table={{
                                columns: ['Date', 'Calls', 'Successful'],
                                rows: stats.daily.map((day) => [
                                    shortDate(day.date),
                                    formatCount(day.calls),
                                    formatCount(day.successful),
                                ]),
                            }}
                        >
                            <DailyCallsChart days={stats.daily} />
                        </ChartCard>
                        <ChartCard
                            title="Calls by hour"
                            caption={`Hours in ${stats.range.timezone}`}
                            table={{
                                columns: ['Hour', 'Calls'],
                                rows: busyHours.length
                                    ? busyHours.map((hour) => [hourLabel(hour.hour), formatCount(hour.calls)])
                                    : [['No calls', '0']],
                            }}
                        >
                            <HourlyCallsChart hours={stats.hourly} />
                        </ChartCard>
                    </div>

                    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">{breakdownCards}</div>
                </>
            )}
        </div>
    );
}
