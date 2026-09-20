/**
 * Date ranges for the call dashboard.
 *
 * Every range is a pair of plain `YYYY-MM-DD` strings in the *organization's*
 * timezone, inclusive at both ends — the same shape the stats API takes. Working
 * in date strings, not `Date` objects, keeps "today" from drifting when the
 * browser's timezone differs from the organization's (a viewer abroad still sees
 * the organization's day) and keeps DST out of the arithmetic.
 */

export type RangePreset = 'today' | '7d' | '30d' | 'custom';

export interface DateRange {
    start: string;
    end: string;
}

// The API refuses anything longer.
export const MAX_RANGE_DAYS = 366;

const pad = (value: number) => String(value).padStart(2, '0');

/** A local `Date` as `YYYY-MM-DD`, read in the browser's own timezone. */
export function formatLocalDate(date: Date): string {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The calendar day it currently is in `timeZone`, as `YYYY-MM-DD`. */
export function todayInTimezone(now: Date, timeZone: string): string {
    try {
        // The en-CA locale formats dates as YYYY-MM-DD.
        return new Intl.DateTimeFormat('en-CA', {
            timeZone,
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
        }).format(now);
    } catch {
        // An unrecognised timezone: fall back to the browser's own day.
        return formatLocalDate(now);
    }
}

function parts(date: string): [number, number, number] {
    const [year, month, day] = date.split('-').map(Number);
    return [year, month, day];
}

export function addDays(date: string, days: number): string {
    const [year, month, day] = parts(date);
    return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

/** Inclusive number of days in a range. */
export function rangeDays(range: DateRange): number {
    const [sy, sm, sd] = parts(range.start);
    const [ey, em, ed] = parts(range.end);
    return Math.round((Date.UTC(ey, em - 1, ed) - Date.UTC(sy, sm - 1, sd)) / 86_400_000) + 1;
}

/** The range for a preset, given today's date in the organization's timezone. */
export function presetRange(preset: Exclude<RangePreset, 'custom'>, today: string): DateRange {
    switch (preset) {
        case 'today':
            return { start: today, end: today };
        case '7d':
            return { start: addDays(today, -6), end: today };
        case '30d':
            return { start: addDays(today, -29), end: today };
    }
}

/** A `YYYY-MM-DD` string as a local-midnight `Date`, for the calendar control. */
export function toLocalDate(date: string): Date {
    const [year, month, day] = parts(date);
    return new Date(year, month - 1, day);
}

/** Whether a range is one the API will accept. */
export function isServableRange(range: DateRange): boolean {
    const days = rangeDays(range);
    return days >= 1 && days <= MAX_RANGE_DAYS;
}

export const PRESET_LABELS: Record<RangePreset, string> = {
    today: 'Today',
    '7d': 'Past 7 days',
    '30d': 'Past 30 days',
    custom: 'Custom',
};
