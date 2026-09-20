'use client';

import { CalendarDays } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { DateRange as DayPickerRange } from 'react-day-picker';

import {
    getCallStatsApiV1OrganizationsReportsCallStatsGet,
    getWorkflowOptionsApiV1OrganizationsReportsWorkflowsGet,
} from '@/client/sdk.gen';
import type { CallStatsResponse } from '@/client/types.gen';
import { Button } from '@/components/ui/button';
import { Calendar } from '@/components/ui/calendar';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { useAuth } from '@/lib/auth';
import { useOrganizationTimezone } from '@/lib/useOrganizationTimezone';

import {
    DateRange,
    formatLocalDate,
    isServableRange,
    MAX_RANGE_DAYS,
    PRESET_LABELS,
    presetRange,
    RangePreset,
    todayInTimezone,
    toLocalDate,
} from './callDashboardRange';
import { CallDashboardView } from './CallDashboardView';
import { shortDate } from './format';

const PRESETS: Array<Exclude<RangePreset, 'custom'>> = ['today', '7d', '30d'];

type CallTypeFilter = 'all' | 'inbound' | 'outbound';

interface AgentOption {
    id: number;
    name: string;
}

interface CallDashboardProps {
    // Browser test sessions are left out by default. The people who build agents
    // test them in the browser, so they can switch them in; clients never see this.
    showTestCallsSwitch?: boolean;
}

/**
 * The client dashboard: one row of filters above everything they scope, then the
 * KPIs and charts for the chosen range. The range is always interpreted in the
 * organization's timezone, so "Today" is the organization's today.
 */
export function CallDashboard({ showTestCallsSwitch = false }: CallDashboardProps) {
    const auth = useAuth();

    const timezone = useOrganizationTimezone();
    const [preset, setPreset] = useState<RangePreset>('7d');
    const [custom, setCustom] = useState<DayPickerRange | undefined>();
    const [customOpen, setCustomOpen] = useState(false);
    // The first day of a custom range, picked while waiting for the second.
    const [pendingStart, setPendingStart] = useState<Date | null>(null);
    const [agentId, setAgentId] = useState<string>('all');
    const [callType, setCallType] = useState<CallTypeFilter>('all');
    const [includeTestCalls, setIncludeTestCalls] = useState(false);
    const [agents, setAgents] = useState<AgentOption[]>([]);

    const [stats, setStats] = useState<CallStatsResponse | null>(null);
    const [loadedOnce, setLoadedOnce] = useState(false);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const latestRequest = useRef(0);

    useEffect(() => {
        if (!auth.isAuthenticated || auth.loading) return;
        let cancelled = false;
        (async () => {
            try {
                const response = await getWorkflowOptionsApiV1OrganizationsReportsWorkflowsGet({});
                if (!cancelled && response.data) setAgents(response.data);
            } catch (err) {
                console.error('Failed to load agents for the dashboard filter:', err);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [auth.isAuthenticated, auth.loading]);

    const today = useMemo(() => (timezone ? todayInTimezone(new Date(), timezone) : null), [timezone]);

    const range: DateRange | null = useMemo(() => {
        if (!today) return null;
        if (preset !== 'custom') return presetRange(preset, today);
        if (custom?.from && custom?.to) {
            return { start: formatLocalDate(custom.from), end: formatLocalDate(custom.to) };
        }
        return null;
    }, [preset, custom, today]);

    const rangeTooLong = range !== null && !isServableRange(range);
    // The request depends on the two dates, not on the object that holds them: the
    // range object is rebuilt whenever the calendar selection changes (even for an
    // unchanged range), and that must not trigger a refetch.
    const rangeStart = range?.start ?? null;
    const rangeEnd = range?.end ?? null;

    useEffect(() => {
        if (!auth.isAuthenticated || auth.loading) return;

        if (!rangeStart || !rangeEnd || !timezone || rangeTooLong) {
            // Whatever is still in flight was for a range that is no longer shown:
            // its answer must not appear under this one.
            latestRequest.current += 1;
            setRefreshing(false);
            return;
        }

        const requestId = ++latestRequest.current;
        setRefreshing(true);
        setError(null);

        (async () => {
            try {
                const response = await getCallStatsApiV1OrganizationsReportsCallStatsGet({
                    query: {
                        start_date: rangeStart,
                        end_date: rangeEnd,
                        timezone,
                        ...(agentId !== 'all' && { workflow_id: Number(agentId) }),
                        ...(callType !== 'all' && { call_type: callType }),
                        ...(showTestCallsSwitch && includeTestCalls && { include_test_calls: true }),
                    },
                });
                // A newer request has been made since; this answer is stale.
                if (requestId !== latestRequest.current) return;
                if (response.data) {
                    setStats(response.data);
                } else {
                    setError('The dashboard could not be loaded. Please try again.');
                }
            } catch {
                if (requestId === latestRequest.current) {
                    setError('The dashboard could not be loaded. Please try again.');
                }
            } finally {
                if (requestId === latestRequest.current) {
                    setRefreshing(false);
                    setLoadedOnce(true);
                }
            }
        })();
    }, [
        auth.isAuthenticated,
        auth.loading,
        rangeStart,
        rangeEnd,
        timezone,
        agentId,
        callType,
        includeTestCalls,
        showTestCallsSwitch,
        rangeTooLong,
    ]);

    const customLabel =
        preset === 'custom' && range ? `${shortDate(range.start)} – ${shortDate(range.end)}` : PRESET_LABELS.custom;

    // A range takes two clicks: the first sets the start, the second the end (in
    // either order; the same day twice is a single-day range). The calendar's own
    // range selection is not used: on the first click it reports a one-day range
    // already, which would apply immediately and close the picker.
    const handleDayClick = (day: Date) => {
        if (!pendingStart) {
            setPendingStart(day);
            return;
        }
        const [from, to] = day < pendingStart ? [day, pendingStart] : [pendingStart, day];
        setPendingStart(null);
        setCustom({ from, to });
        setPreset('custom');
        if (isServableRange({ start: formatLocalDate(from), end: formatLocalDate(to) })) {
            setCustomOpen(false);
        }
    };

    const handleCustomOpenChange = (open: boolean) => {
        setCustomOpen(open);
        // Closing half-way through a pick abandons it rather than leaving a stray start day.
        if (!open) setPendingStart(null);
    };

    return (
        <section className="space-y-6" aria-label="Call dashboard">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                    {PRESETS.map((option) => (
                        <Button
                            key={option}
                            size="sm"
                            variant={preset === option ? 'default' : 'outline'}
                            aria-pressed={preset === option}
                            onClick={() => setPreset(option)}
                        >
                            {PRESET_LABELS[option]}
                        </Button>
                    ))}
                    <Popover open={customOpen} onOpenChange={handleCustomOpenChange}>
                        <PopoverTrigger asChild>
                            <Button
                                size="sm"
                                variant={preset === 'custom' ? 'default' : 'outline'}
                                aria-pressed={preset === 'custom'}
                                className="gap-2"
                            >
                                <CalendarDays className="h-4 w-4" />
                                {customLabel}
                            </Button>
                        </PopoverTrigger>
                        <PopoverContent align="start" className="w-auto p-0">
                            <Calendar
                                mode="range"
                                numberOfMonths={2}
                                // Two months sit side by side, so each day appears once; the neighbouring
                                // month's days would otherwise repeat (and be highlighted) in the other panel.
                                showOutsideDays={false}
                                selected={pendingStart ? { from: pendingStart, to: pendingStart } : custom}
                                onDayClick={handleDayClick}
                                defaultMonth={custom?.from ?? (today ? toLocalDate(today) : undefined)}
                                disabled={today ? { after: toLocalDate(today) } : undefined}
                            />
                            <p className="border-t border-border px-4 py-2 text-xs text-muted-foreground">
                                {pendingStart
                                    ? 'Now pick the last day.'
                                    : `Pick a start and an end day, up to ${MAX_RANGE_DAYS} days.`}
                            </p>
                        </PopoverContent>
                    </Popover>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                    <Select value={agentId} onValueChange={setAgentId}>
                        <SelectTrigger className="w-[200px]" aria-label="Agent">
                            <SelectValue placeholder="All agents" />
                        </SelectTrigger>
                        <SelectContent>
                            <SelectItem value="all">All agents</SelectItem>
                            {agents.map((agent) => (
                                <SelectItem key={agent.id} value={String(agent.id)}>
                                    {agent.name}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    <Select value={callType} onValueChange={(value) => setCallType(value as CallTypeFilter)}>
                        <SelectTrigger className="w-[150px]" aria-label="Call type">
                            <SelectValue placeholder="All calls" />
                        </SelectTrigger>
                        <SelectContent>
                            <SelectItem value="all">All calls</SelectItem>
                            <SelectItem value="inbound">Inbound</SelectItem>
                            <SelectItem value="outbound">Outbound</SelectItem>
                        </SelectContent>
                    </Select>
                    {showTestCallsSwitch && (
                        <div className="flex items-center gap-2 pl-1">
                            <Switch
                                id="include-test-calls"
                                checked={includeTestCalls}
                                onCheckedChange={setIncludeTestCalls}
                            />
                            <Label htmlFor="include-test-calls" className="text-sm font-normal text-muted-foreground">
                                Include test calls
                            </Label>
                        </div>
                    )}
                </div>
            </div>

            {rangeTooLong ? (
                <p role="alert" className="text-sm text-destructive">
                    Choose a range of up to {MAX_RANGE_DAYS} days.
                </p>
            ) : preset === 'custom' && !range ? (
                <p className="text-sm text-muted-foreground">Pick a start and an end day to see the dashboard.</p>
            ) : !loadedOnce && !stats && !error ? (
                <div className="space-y-4" aria-busy="true">
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                        {Array.from({ length: 5 }).map((_, index) => (
                            <Skeleton key={index} className="h-28 rounded-xl" />
                        ))}
                    </div>
                    <Skeleton className="h-72 rounded-xl" />
                </div>
            ) : (
                <CallDashboardView stats={stats} refreshing={refreshing} error={error} />
            )}

            {timezone && (
                <p className="text-xs text-muted-foreground">
                    Days and hours are shown in {timezone}.{' '}
                    {showTestCallsSwitch && includeTestCalls
                        ? 'Browser test sessions are included.'
                        : 'Browser test sessions are not counted.'}
                </p>
            )}
        </section>
    );
}
