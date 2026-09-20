import { describe, expect, it } from 'vitest';

import { formatDateTime12h } from './dateTime';

describe('formatDateTime12h', () => {
    it('shows the organization timezone on a 12-hour clock', () => {
        // 09:37:52 UTC is 3:07:52 PM in Kolkata (UTC+5:30).
        expect(formatDateTime12h('2026-09-17T09:37:52.783+00:00', 'Asia/Kolkata')).toBe('17 Sep 2026, 3:07:52 PM');
    });

    it('does not depend on the viewer: the same instant reads differently per timezone', () => {
        const instant = '2026-09-17T18:30:00Z';

        expect(formatDateTime12h(instant, 'Asia/Kolkata')).toBe('18 Sep 2026, 12:00:00 AM');
        expect(formatDateTime12h(instant, 'UTC')).toBe('17 Sep 2026, 6:30:00 PM');
        expect(formatDateTime12h(instant, 'America/Los_Angeles')).toBe('17 Sep 2026, 11:30:00 AM');
    });

    it('marks noon as PM and midnight as AM', () => {
        expect(formatDateTime12h('2026-09-17T12:00:00Z', 'UTC')).toBe('17 Sep 2026, 12:00:00 PM');
        expect(formatDateTime12h('2026-09-17T00:00:05Z', 'UTC')).toBe('17 Sep 2026, 12:00:05 AM');
    });

    it('keeps a stable short month name', () => {
        expect(formatDateTime12h('2026-09-05T10:00:00Z', 'UTC')).toContain(' Sep ');
    });

    it('falls back rather than throwing for an unknown timezone', () => {
        expect(formatDateTime12h('2026-09-17T09:37:52Z', 'Mars/Olympus')).toMatch(/^17 Sep 2026, \d{1,2}:\d{2}:52 (AM|PM)$/);
    });

    it('returns a dash for missing or invalid values', () => {
        expect(formatDateTime12h(null, 'UTC')).toBe('—');
        expect(formatDateTime12h(undefined, 'UTC')).toBe('—');
        expect(formatDateTime12h('not a date', 'UTC')).toBe('—');
    });
});
