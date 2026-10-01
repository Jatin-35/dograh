'use client';

import { Check, CheckCircle2, Clock, Copy, type LucideIcon,MinusCircle, RotateCw, XCircle } from 'lucide-react';
import { useState } from 'react';

import { cn } from '@/lib/utils';

export const CONDITIONAL_WEBHOOK_PREFIX = 'conditional_webhook_';

type Result = Record<string, unknown>;

type StatusStyle = { label: string; icon: LucideIcon; badge: string; icon_color: string };

const STATUS: Record<string, StatusStyle> = {
    delivered: {
        label: 'Delivered',
        icon: CheckCircle2,
        badge: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
        icon_color: 'text-emerald-600 dark:text-emerald-400',
    },
    failed: {
        label: 'Failed',
        icon: XCircle,
        badge: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
        icon_color: 'text-red-600 dark:text-red-400',
    },
    retrying: {
        label: 'Retrying',
        icon: RotateCw,
        badge: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
        icon_color: 'text-amber-600 dark:text-amber-400',
    },
    queued: {
        label: 'Queued',
        icon: Clock,
        badge: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
        icon_color: 'text-amber-600 dark:text-amber-400',
    },
    not_sent: {
        label: 'Not sent',
        icon: MinusCircle,
        badge: 'bg-muted text-muted-foreground',
        icon_color: 'text-muted-foreground',
    },
};

const REASONS: Record<string, string> = {
    conditions_not_met: 'Not sent: its "Send only if" rules did not hold for this call.',
    disabled: 'Not sent: the node is disabled.',
    invalid_configuration: 'Not sent: the node is not configured correctly.',
};

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Entries written before statuses existed only carry `sent`. */
export function webhookStatus(result: Result): string {
    if (typeof result.status === 'string') return result.status;
    return result.sent === true ? 'queued' : 'not_sent';
}

/** Show JSON text as indented JSON; anything else as it is. */
export function prettyBody(value: unknown): string {
    if (typeof value === 'string') {
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch {
            return value;
        }
    }
    return JSON.stringify(value, null, 2);
}

/**
 * The receiver's own explanation, pulled out of its reply when it has one —
 * e.g. WhatsApp's "(#132001) Template name does not exist in the translation".
 */
export function receiverMessage(response: unknown): string | null {
    if (typeof response !== 'string') return null;
    let body: unknown;
    try {
        body = JSON.parse(response);
    } catch {
        return null;
    }
    const candidates: unknown[] = [];
    if (isRecord(body)) {
        const detail = body.detail;
        const error = body.error;
        if (isRecord(detail)) candidates.push(detail.message);
        if (typeof detail === 'string') candidates.push(detail);
        if (isRecord(error)) candidates.push(error.message);
        if (typeof error === 'string') candidates.push(error);
        candidates.push(body.message);
    }
    const found = candidates.find((c) => typeof c === 'string' && c.trim());
    return typeof found === 'string' ? found : null;
}

function CopyButton({ text }: { text: string }) {
    const [copied, setCopied] = useState(false);
    return (
        <button
            type="button"
            onClick={async () => {
                try {
                    await navigator.clipboard.writeText(text);
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                } catch {
                    // Clipboard unavailable (insecure context); nothing to recover.
                }
            }}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="Copy"
        >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? 'Copied' : 'Copy'}
        </button>
    );
}

function Panel({ title, meta, body }: { title: string; meta?: string; body: string }) {
    return (
        <div className="min-w-0 space-y-1.5">
            <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    {title}
                    {meta && <span className="ml-2 normal-case tracking-normal">{meta}</span>}
                </p>
                <CopyButton text={body} />
            </div>
            {/* JSON keeps its own lines; long values scroll sideways instead of
                breaking mid-word. */}
            <pre className="max-h-72 overflow-auto whitespace-pre rounded-md border border-border bg-muted/60 p-3 font-mono text-xs leading-relaxed">
                {body}
            </pre>
        </div>
    );
}

export function ConditionalWebhookResult({ result }: { result: Result }) {
    const status = webhookStatus(result);
    const style = STATUS[status] ?? STATUS.not_sent;
    const Icon = style.icon;
    const request = isRecord(result.request) ? result.request : null;
    const failedConditions = Array.isArray(result.failed_conditions) ? result.failed_conditions : [];
    const hasResponse = result.response !== undefined || result.http_status != null;
    const problem =
        status === 'failed' || status === 'retrying'
            ? receiverMessage(result.response) ?? (typeof result.error === 'string' ? result.error : null)
            : null;

    const responseMeta = [
        result.http_status != null ? `HTTP ${String(result.http_status)}` : null,
        typeof result.attempts === 'number' ? `attempt ${result.attempts}` : null,
    ]
        .filter(Boolean)
        .join(' · ');

    return (
        <section className="space-y-3 rounded-lg border border-border bg-card p-4">
            <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-start gap-2.5">
                    <Icon className={cn('mt-0.5 h-5 w-5 shrink-0', style.icon_color)} />
                    <div className="min-w-0 space-y-1">
                        <h4 className="text-sm font-semibold leading-5 text-foreground">
                            {typeof result.name === 'string' ? result.name : 'Conditional Webhook'}
                        </h4>
                        {request && (
                            <p className="flex min-w-0 items-center gap-2 font-mono text-xs text-muted-foreground">
                                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-semibold text-foreground">
                                    {String(request.method ?? 'POST')}
                                </span>
                                <span className="truncate" title={String(request.url ?? '')}>
                                    {String(request.url ?? '')}
                                </span>
                            </p>
                        )}
                    </div>
                </div>
                <span className={cn('shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium', style.badge)}>
                    {style.label}
                </span>
            </div>

            {status === 'not_sent' && (
                <div className="space-y-1.5 rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
                    <p>{REASONS[String(result.reason)] ?? 'Not sent.'}</p>
                    {failedConditions.length > 0 && (
                        <ul className="space-y-1">
                            {failedConditions.map((condition) => (
                                <li key={String(condition)} className="flex items-center gap-2">
                                    <XCircle className="h-3.5 w-3.5 shrink-0" />
                                    <code className="font-mono text-xs">{String(condition)}</code>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}

            {problem && (
                <div
                    className={cn(
                        'break-words rounded-md p-3 text-sm',
                        status === 'failed'
                            ? 'bg-red-50 text-red-800 dark:bg-red-950/50 dark:text-red-300'
                            : 'bg-amber-50 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
                    )}
                >
                    {status === 'retrying' ? `Will retry automatically: ${problem}` : problem}
                </div>
            )}

            {status === 'queued' && (
                <p className="text-sm text-muted-foreground">Sent off; waiting for the receiver to answer.</p>
            )}

            {(request?.payload !== undefined || hasResponse) && (
                <div className="grid gap-3 lg:grid-cols-2">
                    {request?.payload !== undefined && <Panel title="Request" body={prettyBody(request.payload)} />}
                    {hasResponse && (
                        <Panel
                            title="Response"
                            meta={responseMeta || undefined}
                            body={result.response !== undefined ? prettyBody(result.response) : '—'}
                        />
                    )}
                </div>
            )}
        </section>
    );
}

export function ConditionalWebhookResults({ entries }: { entries: [string, Result][] }) {
    if (entries.length === 0) return null;
    return (
        <div className="space-y-3">
            <h3 className="text-sm font-semibold text-foreground">
                Webhooks <span className="font-normal text-muted-foreground">({entries.length})</span>
            </h3>
            {entries.map(([key, result]) => (
                <ConditionalWebhookResult key={key} result={result} />
            ))}
        </div>
    );
}
