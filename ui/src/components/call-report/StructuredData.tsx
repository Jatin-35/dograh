import { Badge } from '@/components/ui/badge';

function humanizeKey(key: string): string {
    const text = key.replace(/_/g, ' ').trim();
    return text.charAt(0).toUpperCase() + text.slice(1);
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isTagList(value: unknown): value is Array<{ tag: string; reason?: string }> {
    return (
        Array.isArray(value) &&
        value.length > 0 &&
        value.every((item) => isRecord(item) && typeof item.tag === 'string')
    );
}

function Empty({ children = 'None' }: { children?: string }) {
    return <span className="text-sm italic text-muted-foreground">{children}</span>;
}

/**
 * Any JSON value as readable text: keys become labels, booleans become Yes/No,
 * nested objects indent, and QA-style ``[{tag, reason}]`` lists become badges
 * with their reasons. The raw JSON is always one toggle away, so nothing is
 * hidden by formatting — this only makes it legible.
 */
export function StructuredData({ value }: { value: unknown }) {
    if (value === null || value === undefined || value === '') return <Empty>—</Empty>;
    if (typeof value === 'boolean') return <span className="text-sm">{value ? 'Yes' : 'No'}</span>;
    if (typeof value === 'number') return <span className="text-sm tabular-nums">{value}</span>;
    if (typeof value === 'string') return <span className="whitespace-pre-wrap break-words text-sm">{value}</span>;

    if (Array.isArray(value)) {
        if (value.length === 0) return <Empty />;

        if (isTagList(value)) {
            return (
                <ul className="space-y-1.5">
                    {value.map((item, index) => (
                        <li key={`${item.tag}-${index}`} className="flex flex-wrap items-baseline gap-2 text-sm">
                            <Badge variant="outline">{item.tag}</Badge>
                            {item.reason && <span className="text-muted-foreground">{item.reason}</span>}
                        </li>
                    ))}
                </ul>
            );
        }

        if (value.every((item) => typeof item === 'string' || typeof item === 'number')) {
            return (
                <div className="flex flex-wrap gap-1.5">
                    {value.map((item, index) => (
                        <Badge key={`${item}-${index}`} variant="outline">
                            {String(item)}
                        </Badge>
                    ))}
                </div>
            );
        }

        return (
            <ol className="space-y-2">
                {value.map((item, index) => (
                    <li key={index} className="rounded-md border border-border p-2">
                        <StructuredData value={item} />
                    </li>
                ))}
            </ol>
        );
    }

    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <Empty />;

    return (
        <dl className="space-y-2">
            {entries.map(([key, child]) => (
                <div key={key} className="space-y-0.5">
                    <dt className="text-xs font-medium uppercase tracking-[0.1em] text-muted-foreground">
                        {humanizeKey(key)}
                    </dt>
                    <dd className={isRecord(child) ? 'border-l border-border pl-3' : undefined}>
                        <StructuredData value={child} />
                    </dd>
                </div>
            ))}
        </dl>
    );
}
