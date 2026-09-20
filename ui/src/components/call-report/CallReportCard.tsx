'use client';

import {
    CalendarDays,
    Check,
    CheckCircle2,
    ChevronDown,
    ClipboardList,
    Clock,
    Copy,
    Flag,
    Frown,
    Globe,
    HelpCircle,
    ListChecks,
    type LucideIcon,
    Meh,
    Phone,
    PhoneIncoming,
    PhoneOff,
    PhoneOutgoing,
    Smile,
    Sparkles,
    Ticket,
    XCircle,
} from 'lucide-react';
import { ReactNode, useEffect, useState } from 'react';

import { getRunCallReportApiV1WorkflowWorkflowIdRunsRunIdCallReportGet } from '@/client/sdk.gen';
import type { CallReportResponse, NodeAnalysis } from '@/client/types.gen';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/lib/auth';
import { formatDateTime12h } from '@/lib/dateTime';
import { useOrganizationTimezone } from '@/lib/useOrganizationTimezone';
import { cn } from '@/lib/utils';

import { StructuredData } from './StructuredData';

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'outline' | 'success';
type Tone = 'blue' | 'green' | 'violet' | 'amber' | 'rose' | 'slate';

const NOT_AVAILABLE = '—';

// Soft tinted backgrounds for icon chips: one tone per kind of fact, so a report can be
// scanned by colour as well as read. Colour never carries meaning alone — every chip sits
// beside a label.
const TONES: Record<Tone, string> = {
    blue: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
    green: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
    violet: 'bg-violet-500/10 text-violet-600 dark:text-violet-400',
    amber: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
    rose: 'bg-rose-500/10 text-rose-600 dark:text-rose-400',
    slate: 'bg-muted text-muted-foreground',
};

function humanize(value?: string | null): string {
    if (!value) return NOT_AVAILABLE;
    const text = value.replace(/_/g, ' ');
    return text.charAt(0).toUpperCase() + text.slice(1);
}

// A captured value such as "water_tank" reads better as "Water tank". Only plain snake_case
// tokens are touched; anything else (a sentence, a name, Hindi text) is shown as captured,
// and the JSON tab always has the raw value.
function displayCaptured(value: unknown): unknown {
    return typeof value === 'string' && /^[a-z0-9]+(_[a-z0-9]+)+$/.test(value) ? humanize(value) : value;
}

function formatDuration(seconds?: number | null): string {
    if (seconds === null || seconds === undefined) return NOT_AVAILABLE;
    const total = Math.round(seconds);
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return minutes > 0 ? `${minutes}m ${rest}s` : `${rest}s`;
}

function yesNo(value?: boolean | null, unknown = 'Unclear'): string {
    if (value === true) return 'Yes';
    if (value === false) return 'No';
    return unknown;
}

function sentimentVariant(sentiment?: string | null): BadgeVariant {
    if (sentiment === 'positive') return 'success';
    if (sentiment === 'negative') return 'destructive';
    return 'secondary';
}

function outcomeVariant(code: string): BadgeVariant {
    if (code === 'ticket_closed' || code === 'resolved') return 'success';
    if (code === 'failed' || code === 'not_resolved') return 'destructive';
    return 'secondary';
}

function outcomeTone(code: string): Tone {
    if (code === 'ticket_closed' || code === 'resolved') return 'green';
    if (code === 'failed' || code === 'not_resolved') return 'rose';
    if (code === 'no_outcome') return 'slate';
    return 'amber';
}

function disconnectVariant(category: string): BadgeVariant {
    if (category === 'agent_completed') return 'success';
    if (category === 'system_error' || category === 'not_connected') return 'destructive';
    return 'secondary';
}

function disconnectTone(category: string): Tone {
    if (category === 'agent_completed') return 'green';
    if (category === 'system_error' || category === 'not_connected') return 'rose';
    return 'slate';
}

const SENTIMENT_ICONS: Record<string, { icon: LucideIcon; tone: string }> = {
    positive: { icon: Smile, tone: 'text-emerald-600 dark:text-emerald-400' },
    neutral: { icon: Meh, tone: 'text-muted-foreground' },
    negative: { icon: Frown, tone: 'text-rose-600 dark:text-rose-400' },
};

const ANALYSIS_STATUS_LABELS: Record<string, string> = {
    analysed: 'Analysed',
    skipped: 'Skipped',
    error: 'Analysis failed',
    not_run: 'Not analysed',
};

function IconChip({
    icon: Icon,
    tone = 'slate',
    className,
}: {
    icon: LucideIcon;
    tone?: Tone;
    className?: string;
}) {
    return (
        <span className={cn('flex h-9 w-9 shrink-0 items-center justify-center rounded-lg', TONES[tone], className)}>
            <Icon className="h-4 w-4" aria-hidden="true" />
        </span>
    );
}

// A key fact about the call: a tinted icon beside a small label and a bold value.
function Fact({ icon, tone, label, children }: { icon: LucideIcon; tone: Tone; label: string; children: ReactNode }) {
    return (
        <div className="flex items-start gap-3 rounded-xl border border-border/60 bg-muted/30 p-3.5">
            <IconChip icon={icon} tone={tone} />
            <div className="min-w-0 space-y-1">
                <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
                <div className="break-words text-sm font-semibold text-foreground">{children}</div>
            </div>
        </div>
    );
}

// A smaller labelled value, for the detail inside a section.
function Stat({ label, children }: { label: string; children: ReactNode }) {
    return (
        <div className="min-w-0 space-y-1 rounded-lg bg-muted/40 px-3 py-2.5">
            <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
            <div className="break-words text-sm font-medium text-foreground">{children}</div>
        </div>
    );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
    return (
        <div className="min-w-0 space-y-1">
            <p className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
            <div className="break-words text-sm font-medium text-foreground">{children}</div>
        </div>
    );
}

function Section({
    icon,
    tone,
    title,
    aside,
    children,
}: {
    icon: LucideIcon;
    tone: Tone;
    title: string;
    aside?: ReactNode;
    children: ReactNode;
}) {
    return (
        <section className="space-y-4 rounded-xl border border-border/60 p-4">
            <div className="flex flex-wrap items-center gap-2.5">
                <IconChip icon={icon} tone={tone} className="h-7 w-7 rounded-md" />
                <h3 className="text-sm font-semibold text-foreground">{title}</h3>
                {aside}
            </div>
            {children}
        </section>
    );
}

function WithIcon({ icon: Icon, className, children }: { icon: LucideIcon; className?: string; children: ReactNode }) {
    return (
        <span className="inline-flex items-center gap-1.5">
            <Icon className={cn('h-4 w-4', className)} aria-hidden="true" />
            {children}
        </span>
    );
}

function QualityScore({ score }: { score?: number | null }) {
    if (score === null || score === undefined) return <>{NOT_AVAILABLE}</>;
    const percent = Math.max(0, Math.min(100, score * 10));
    const bar = score >= 8 ? 'bg-emerald-500' : score >= 5 ? 'bg-amber-500' : 'bg-rose-500';
    return (
        <span className="flex items-center gap-2">
            <span className="tabular-nums">{score}/10</span>
            <span className="h-1.5 w-16 overflow-hidden rounded-full bg-muted" aria-hidden="true">
                <span className={cn('block h-full rounded-full', bar)} style={{ width: `${percent}%` }} />
            </span>
        </span>
    );
}

function NodeRow({ node }: { node: NodeAnalysis }) {
    const [open, setOpen] = useState(false);
    return (
        <Collapsible open={open} onOpenChange={setOpen} className="rounded-lg border border-border/60 bg-background">
            <CollapsibleTrigger className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm">
                <span className="min-w-0 truncate font-medium">{node.node_name || `Node ${node.node_id}`}</span>
                <span className="flex shrink-0 items-center gap-2">
                    {node.sentiment && (
                        <Badge variant={sentimentVariant(node.sentiment)}>{humanize(node.sentiment)}</Badge>
                    )}
                    <ChevronDown className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`} />
                </span>
            </CollapsibleTrigger>
            <CollapsibleContent className="space-y-3 border-t border-border/60 px-3 py-3">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                    <Field label="Reason">{humanize(node.reason_for_call)}</Field>
                    <Field label="Resolved">{yesNo(node.resolved)}</Field>
                    <Field label="Satisfied">{yesNo(node.satisfied)}</Field>
                    <Field label="Mood">{humanize(node.mood)}</Field>
                    <Field label="Human transfer">{yesNo(node.human_transfer, NOT_AVAILABLE)}</Field>
                    <Field label="Quality score">{node.quality_score ?? NOT_AVAILABLE}</Field>
                </div>
                {node.summary && <p className="text-sm text-muted-foreground">{node.summary}</p>}
                {node.tags && node.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                        {node.tags.map((tag) => (
                            <Badge key={tag} variant="outline">
                                {tag}
                            </Badge>
                        ))}
                    </div>
                )}
            </CollapsibleContent>
        </Collapsible>
    );
}

/**
 * The readable report. Which sections appear follows what the call's agent is set
 * up to do, not a fixed layout: a ticket section on a sales agent, or a "not
 * analysed" line on an agent with no QA node, would read like a fault. What an
 * agent captured during the call is shown under its own names.
 */
export function ReportView({ report, timezone }: { report: CallReportResponse; timezone: string | null }) {
    const { call, disconnect, outcome, ticket, analysis } = report;
    const nodes = analysis.nodes ?? [];
    const tickets = ticket.tickets ?? [];
    const tags = analysis.tags ?? [];
    const captured = Object.entries(report.captured ?? {});
    const analysed = analysis.status === 'analysed';

    const isWebCall = call.is_telephony === false;
    const showTicket = Boolean(report.capabilities?.ticket) || Boolean(ticket.created);
    const showAnalysis = Boolean(report.capabilities?.analysis) || (analysis.status ?? 'not_run') !== 'not_run';
    // With nothing to derive it from, "no outcome recorded" says nothing about the call.
    const showOutcome = showTicket || showAnalysis || outcome.code !== 'no_outcome';

    const callTypeIcon = isWebCall ? Globe : call.call_type === 'outbound' ? PhoneOutgoing : PhoneIncoming;
    const sentiment = analysis.sentiment ? SENTIMENT_ICONS[analysis.sentiment] : undefined;
    const satisfiedIcon =
        analysis.satisfied === true ? CheckCircle2 : analysis.satisfied === false ? XCircle : HelpCircle;
    const satisfiedTone =
        analysis.satisfied === true
            ? 'text-emerald-600 dark:text-emerald-400'
            : analysis.satisfied === false
              ? 'text-rose-600 dark:text-rose-400'
              : 'text-muted-foreground';

    return (
        <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <Fact icon={callTypeIcon} tone={isWebCall ? 'violet' : 'blue'} label="Call type">
                    {isWebCall ? 'Web test call' : humanize(call.call_type)}
                </Fact>
                <Fact icon={CalendarDays} tone="blue" label="Date & time">
                    {formatDateTime12h(call.started_at, timezone)}
                </Fact>
                <Fact icon={Clock} tone="amber" label="Duration">
                    {formatDuration(call.duration_seconds)}
                </Fact>
                <Fact icon={isWebCall ? Globe : Phone} tone="slate" label="Customer number">
                    {call.phone_number || (isWebCall ? 'Web call, no number' : NOT_AVAILABLE)}
                </Fact>
                <Fact icon={PhoneOff} tone={disconnectTone(disconnect.category)} label="Disconnection">
                    <Badge variant={disconnectVariant(disconnect.category)}>{disconnect.label}</Badge>
                </Fact>
                {showOutcome && (
                    <Fact icon={Flag} tone={outcomeTone(outcome.code)} label="Outcome">
                        <Badge variant={outcomeVariant(outcome.code)}>{outcome.label}</Badge>
                    </Fact>
                )}
            </div>

            {captured.length > 0 && (
                <Section
                    icon={ListChecks}
                    tone="violet"
                    title="Captured data"
                    aside={<Badge variant="outline">{captured.length}</Badge>}
                >
                    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                        {captured.map(([key, value]) => (
                            <Stat key={key} label={humanize(key)}>
                                <StructuredData value={displayCaptured(value)} />
                            </Stat>
                        ))}
                    </div>
                </Section>
            )}

            {showTicket && (
                <Section
                    icon={Ticket}
                    tone="amber"
                    title="Ticket"
                    aside={
                        ticket.created ? (
                            <Badge variant={ticket.closed ? 'success' : 'secondary'}>
                                {ticket.closed ? 'All closed' : 'Open'}
                            </Badge>
                        ) : undefined
                    }
                >
                    <div className="grid gap-3 sm:grid-cols-3">
                        <Stat label="Created">{yesNo(ticket.created, 'No')}</Stat>
                        <Stat label="Closed">{ticket.created ? yesNo(ticket.closed, 'No') : NOT_AVAILABLE}</Stat>
                        <Stat label="Tickets">{ticket.count ?? 0}</Stat>
                    </div>
                    {tickets.length > 0 && (
                        <ul className="space-y-2">
                            {tickets.map((item) => (
                                <li
                                    key={item.ticket_id}
                                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/60 bg-background px-3.5 py-3"
                                >
                                    <div className="min-w-0 space-y-1">
                                        <p className="font-mono text-sm font-semibold text-foreground">
                                            <span className="text-muted-foreground">#</span>
                                            {item.ticket_id}
                                        </p>
                                        <p className="text-xs text-muted-foreground">
                                            Created <span>{formatDateTime12h(item.created_at, timezone)}</span>
                                        </p>
                                        {item.closed && (
                                            <p className="text-xs text-muted-foreground">
                                                Closed <span>{formatDateTime12h(item.closed_at, timezone)}</span>
                                            </p>
                                        )}
                                    </div>
                                    <Badge variant={item.closed ? 'success' : 'secondary'}>
                                        {item.closed ? 'Closed' : 'Open'}
                                    </Badge>
                                </li>
                            ))}
                        </ul>
                    )}
                </Section>
            )}

            {showAnalysis && (
                <Section
                    icon={Sparkles}
                    tone="violet"
                    title="Call analysis"
                    aside={
                        <Badge variant={analysed ? 'outline' : 'secondary'}>
                            {ANALYSIS_STATUS_LABELS[analysis.status ?? 'not_run'] ?? humanize(analysis.status)}
                        </Badge>
                    }
                >
                    {analysed ? (
                        <>
                            {analysis.summary && (
                                <blockquote className="rounded-r-lg border-l-4 border-violet-500/40 bg-violet-500/5 px-4 py-3 text-sm leading-relaxed text-foreground">
                                    {analysis.summary}
                                </blockquote>
                            )}
                            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                                <Stat label="Reason for call">{humanize(analysis.reason_for_call)}</Stat>
                                <Stat label="Sentiment">
                                    {analysis.sentiment && sentiment ? (
                                        <WithIcon icon={sentiment.icon} className={sentiment.tone}>
                                            {humanize(analysis.sentiment)}
                                        </WithIcon>
                                    ) : (
                                        NOT_AVAILABLE
                                    )}
                                </Stat>
                                <Stat label="Customer satisfied">
                                    <WithIcon icon={satisfiedIcon} className={satisfiedTone}>
                                        {yesNo(analysis.satisfied)}
                                    </WithIcon>
                                </Stat>
                                <Stat label="Issue resolved">{yesNo(analysis.resolved)}</Stat>
                                <Stat label="Transferred to a person">
                                    {yesNo(analysis.human_transfer, NOT_AVAILABLE)}
                                </Stat>
                                <Stat label="Quality score">
                                    <QualityScore score={analysis.quality_score} />
                                </Stat>
                            </div>
                            {tags.length > 0 && (
                                <div className="flex flex-wrap gap-1.5">
                                    {tags.map((tag) => (
                                        <Badge key={tag} variant="outline">
                                            {tag}
                                        </Badge>
                                    ))}
                                </div>
                            )}
                            {nodes.length > 1 && (
                                <div className="space-y-2">
                                    <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
                                        By node
                                    </p>
                                    {nodes.map((node) => (
                                        <NodeRow key={node.node_id} node={node} />
                                    ))}
                                </div>
                            )}
                        </>
                    ) : (
                        <p className="text-sm text-muted-foreground">
                            {analysis.status === 'skipped' && analysis.skipped_reason
                                ? `Call analysis was skipped: ${analysis.skipped_reason}.`
                                : 'No call analysis is available for this call.'}
                        </p>
                    )}
                </Section>
            )}
        </div>
    );
}

interface CallReportCardProps {
    workflowId: number;
    runId: number;
    // Called once the report has loaded, so the page can react to it (for
    // instance by withholding raw data when the number was masked for this viewer).
    onLoaded?: (report: CallReportResponse) => void;
}

export function CallReportCard({ workflowId, runId, onLoaded }: CallReportCardProps) {
    const auth = useAuth();
    const timezone = useOrganizationTimezone();
    const [report, setReport] = useState<CallReportResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [failed, setFailed] = useState(false);
    const [copied, setCopied] = useState(false);

    useEffect(() => {
        if (!auth.isAuthenticated || auth.loading) return;
        let cancelled = false;

        (async () => {
            setLoading(true);
            setFailed(false);
            try {
                const response = await getRunCallReportApiV1WorkflowWorkflowIdRunsRunIdCallReportGet({
                    path: { workflow_id: workflowId, run_id: runId },
                });
                if (cancelled) return;
                if (response.data) {
                    setReport(response.data);
                    onLoaded?.(response.data);
                } else {
                    setFailed(true);
                }
            } catch {
                if (!cancelled) setFailed(true);
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();

        return () => {
            cancelled = true;
        };
        // onLoaded is a fresh function each render; the fetch must run only when the run changes.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [auth.isAuthenticated, auth.loading, workflowId, runId]);

    const json = report ? JSON.stringify(report, null, 2) : '';

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(json);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch {
            // Clipboard can be unavailable (insecure context); nothing to recover.
        }
    };

    return (
        <Card className="border-border">
            <Tabs defaultValue="report">
                <CardHeader className="flex-row items-center justify-between gap-4 space-y-0 pb-4">
                    <div className="flex items-center gap-3">
                        <IconChip icon={ClipboardList} tone="blue" />
                        <div className="space-y-0.5">
                            <CardTitle className="text-lg">Call Report</CardTitle>
                            {timezone && (
                                <CardDescription className="text-xs">Times shown in {timezone}</CardDescription>
                            )}
                        </div>
                    </div>
                    <TabsList>
                        <TabsTrigger value="report">Report</TabsTrigger>
                        <TabsTrigger value="json">JSON</TabsTrigger>
                    </TabsList>
                </CardHeader>
                <CardContent>
                    {loading ? (
                        <div className="space-y-3">
                            <Skeleton className="h-4 w-full" />
                            <Skeleton className="h-4 w-3/4" />
                            <Skeleton className="h-4 w-1/2" />
                        </div>
                    ) : failed || !report ? (
                        <p className="text-sm text-muted-foreground">The call report could not be loaded.</p>
                    ) : (
                        <>
                            <TabsContent value="report" className="mt-0">
                                <ReportView report={report} timezone={timezone} />
                            </TabsContent>
                            <TabsContent value="json" className="mt-0 space-y-2">
                                <div className="flex justify-end">
                                    <Button variant="ghost" size="sm" onClick={handleCopy} className="gap-2">
                                        {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                                        {copied ? 'Copied' : 'Copy'}
                                    </Button>
                                </div>
                                <pre className="max-h-[32rem] overflow-auto rounded-md bg-muted p-3 text-sm">{json}</pre>
                            </TabsContent>
                        </>
                    )}
                </CardContent>
            </Tabs>
        </Card>
    );
}
