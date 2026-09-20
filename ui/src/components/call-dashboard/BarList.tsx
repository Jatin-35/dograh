import { formatCount, shareOf } from './format';

export interface BarListItem {
    key: string;
    label: string;
    count: number;
}

interface BarListProps {
    items: BarListItem[];
    emptyLabel: string;
    maxRows?: number;
}

const OTHER_KEY = '__other__';

// Past this many rows a category list stops being readable, so the tail folds
// into one "Other" row rather than growing another colour.
function foldTail(items: BarListItem[], maxRows: number): BarListItem[] {
    if (items.length <= maxRows) return items;
    const head = items.slice(0, maxRows - 1);
    const rest = items.slice(maxRows - 1).reduce((sum, item) => sum + item.count, 0);
    return [...head, { key: OTHER_KEY, label: 'Other', count: rest }];
}

/**
 * Ranked horizontal bars for nominal categories. Every bar is the same colour:
 * the categories have no order, so length alone carries the value and hue is
 * left free (a value ramp here would say the same thing twice).
 */
export function BarList({ items, emptyLabel, maxRows = 8 }: BarListProps) {
    if (items.length === 0) {
        return <p className="py-8 text-center text-sm text-muted-foreground">{emptyLabel}</p>;
    }

    const rows = foldTail(items, maxRows);
    const total = items.reduce((sum, item) => sum + item.count, 0);
    const max = Math.max(...rows.map((row) => row.count), 1);

    return (
        <ul className="space-y-3">
            {rows.map((row) => (
                <li key={row.key} title={`${row.label}: ${formatCount(row.count)} (${shareOf(row.count, total)})`}>
                    <div className="mb-1 flex items-baseline justify-between gap-3 text-sm">
                        <span className="truncate text-foreground">{row.label}</span>
                        <span className="shrink-0 tabular-nums text-muted-foreground">{formatCount(row.count)}</span>
                    </div>
                    <div className="h-2" aria-hidden="true">
                        <div
                            className="h-2 rounded-r-[4px]"
                            style={{
                                width: `${(row.count / max) * 100}%`,
                                minWidth: row.count > 0 ? 4 : 0,
                                background: row.key === OTHER_KEY ? 'var(--cd-neutral)' : 'var(--cd-series)',
                            }}
                        />
                    </div>
                </li>
            ))}
        </ul>
    );
}
