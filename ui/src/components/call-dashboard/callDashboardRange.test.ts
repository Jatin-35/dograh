import { describe, expect, it } from 'vitest';

import {
    addDays,
    formatLocalDate,
    isServableRange,
    MAX_RANGE_DAYS,
    presetRange,
    rangeDays,
    todayInTimezone,
    toLocalDate,
} from './callDashboardRange';

describe('todayInTimezone', () => {
    // 20:00 UTC on the 17th is already 01:30 on the 18th in Kolkata.
    const instant = new Date('2026-09-17T20:00:00Z');

    it('follows the organization timezone, not the browser', () => {
        expect(todayInTimezone(instant, 'Asia/Kolkata')).toBe('2026-09-18');
        expect(todayInTimezone(instant, 'UTC')).toBe('2026-09-17');
        expect(todayInTimezone(instant, 'America/Los_Angeles')).toBe('2026-09-17');
    });

    it('falls back to the local day for an unknown timezone', () => {
        expect(todayInTimezone(instant, 'Mars/Olympus')).toBe(formatLocalDate(instant));
    });
});

describe('addDays', () => {
    it('crosses month and year boundaries', () => {
        expect(addDays('2026-09-01', -1)).toBe('2026-08-31');
        expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
        expect(addDays('2028-02-28', 1)).toBe('2028-02-29');
    });
});

describe('presetRange', () => {
    it('gives inclusive ranges ending today', () => {
        expect(presetRange('today', '2026-09-18')).toEqual({ start: '2026-09-18', end: '2026-09-18' });
        expect(presetRange('7d', '2026-09-18')).toEqual({ start: '2026-09-12', end: '2026-09-18' });
        expect(presetRange('30d', '2026-09-18')).toEqual({ start: '2026-08-20', end: '2026-09-18' });
    });

    it('spans exactly 1, 7 and 30 days', () => {
        expect(rangeDays(presetRange('today', '2026-09-18'))).toBe(1);
        expect(rangeDays(presetRange('7d', '2026-09-18'))).toBe(7);
        expect(rangeDays(presetRange('30d', '2026-09-18'))).toBe(30);
    });
});

describe('isServableRange', () => {
    it('accepts up to the API limit and rejects beyond it or reversed', () => {
        expect(isServableRange({ start: '2026-09-18', end: '2026-09-18' })).toBe(true);
        expect(isServableRange({ start: addDays('2026-09-18', -(MAX_RANGE_DAYS - 1)), end: '2026-09-18' })).toBe(true);
        expect(isServableRange({ start: addDays('2026-09-18', -MAX_RANGE_DAYS), end: '2026-09-18' })).toBe(false);
        expect(isServableRange({ start: '2026-09-19', end: '2026-09-18' })).toBe(false);
    });
});

describe('calendar conversion', () => {
    it('round-trips through a local Date without shifting the day', () => {
        expect(formatLocalDate(toLocalDate('2026-03-08'))).toBe('2026-03-08');
        expect(formatLocalDate(toLocalDate('2026-11-01'))).toBe('2026-11-01');
    });
});
