const NOT_AVAILABLE = '—';

/**
 * A moment as `17 Sep 2026, 3:07:52 PM` in the given IANA timezone (12-hour clock).
 *
 * The timezone should be the organization's, not the viewer's browser: a client
 * reading a call time abroad still sees it in the organization's own day, matching
 * the dashboard. Built from the formatter's parts so the shape does not change
 * with the browser's locale data (some print "Sept", some use a narrow space
 * before AM/PM). An unknown timezone falls back to the browser's rather than failing.
 */
export function formatDateTime12h(iso?: string | null, timeZone?: string | null): string {
    if (!iso) return NOT_AVAILABLE;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return NOT_AVAILABLE;

    const options: Intl.DateTimeFormatOptions = {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        hour12: true,
    };

    let parts: Intl.DateTimeFormatPart[];
    try {
        parts = new Intl.DateTimeFormat('en-US', { ...options, ...(timeZone ? { timeZone } : {}) }).formatToParts(date);
    } catch {
        parts = new Intl.DateTimeFormat('en-US', options).formatToParts(date);
    }

    const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? '';
    return `${get('day')} ${get('month')} ${get('year')}, ${get('hour')}:${get('minute')}:${get('second')} ${get('dayPeriod').toUpperCase()}`;
}
