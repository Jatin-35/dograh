import { formatCount, shareOf } from './format';

export type Tone = 'negative' | 'neutral' | 'positive';

export interface DivergingSegment {
    key: string;
    label: string;
    count: number;
    tone: Tone;
}

const TONE_COLOR: Record<Tone, string> = {
    negative: 'var(--cd-negative)',
    neutral: 'var(--cd-neutral)',
    positive: 'var(--cd-series)',
};

interface DivergingBarProps {
    // Ordered from the negative pole to the positive one.
    segments: DivergingSegment[];
    emptyLabel: string;
}

/**
 * One stacked bar for an ordered scale (unhappy … happy): two opposite hues with
 * a grey middle, centred on neutral. Every segment is also named in the legend
 * with its count and share, so colour never carries the meaning alone.
 */
export function DivergingBar({ segments, emptyLabel }: DivergingBarProps) {
    const total = segments.reduce((sum, segment) => sum + segment.count, 0);
    if (total === 0) {
        return <p className="py-8 text-center text-sm text-muted-foreground">{emptyLabel}</p>;
    }

    const summary = segments.map((s) => `${s.label} ${s.count}`).join(', ');

    return (
        <div>
            <div className="flex h-3 w-full gap-[2px] overflow-hidden rounded-[4px]" role="img" aria-label={summary}>
                {segments
                    .filter((segment) => segment.count > 0)
                    .map((segment) => (
                        <div
                            key={segment.key}
                            style={{ flex: segment.count, background: TONE_COLOR[segment.tone] }}
                            title={`${segment.label}: ${formatCount(segment.count)} (${shareOf(segment.count, total)})`}
                        />
                    ))}
            </div>
            <ul className="mt-4 space-y-2 text-sm">
                {segments.map((segment) => (
                    <li key={segment.key} className="flex items-center justify-between gap-3">
                        <span className="flex items-center gap-2 text-foreground">
                            <span
                                className="h-2.5 w-2.5 shrink-0 rounded-full"
                                style={{ background: TONE_COLOR[segment.tone] }}
                                aria-hidden="true"
                            />
                            {segment.label}
                        </span>
                        <span className="tabular-nums text-muted-foreground">
                            {formatCount(segment.count)} · {shareOf(segment.count, total)}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
