export function formatCount(value: number): string {
    return value.toLocaleString();
}

export function formatDuration(seconds?: number | null): string {
    if (seconds === null || seconds === undefined) return '—';
    const total = Math.round(seconds);
    if (total < 60) return `${total}s`;
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m ${rest}s`;
}

export function formatPercent(value?: number | null): string {
    return value === null || value === undefined ? '—' : `${value}%`;
}

export function shareOf(count: number, total: number): string {
    return total > 0 ? `${Math.round((count / total) * 100)}%` : '0%';
}

/** A `YYYY-MM-DD` day as a short label such as "18 Sep". */
export function shortDate(date: string): string {
    const [year, month, day] = date.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** A `YYYY-MM-DD` day with the weekday, such as "Fri, 18 Sep 2026". */
export function longDate(date: string): string {
    const [year, month, day] = date.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString(undefined, {
        weekday: 'short',
        day: 'numeric',
        month: 'short',
        year: 'numeric',
    });
}

export function hourLabel(hour: number): string {
    return `${String(hour).padStart(2, '0')}:00`;
}
