import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CallStatsResponse } from '@/client/types.gen';

const h = vi.hoisted(() => ({
    timezone: 'Asia/Kolkata' as string | null,
    auth: { isAuthenticated: true, loading: false },
    getStats: vi.fn(),
    getAgents: vi.fn(),
}));

vi.mock('@/client/sdk.gen', () => ({
    getCallStatsApiV1OrganizationsReportsCallStatsGet: (...args: unknown[]) => h.getStats(...args),
    getWorkflowOptionsApiV1OrganizationsReportsWorkflowsGet: (...args: unknown[]) => h.getAgents(...args),
}));
vi.mock('@/lib/auth', () => ({ useAuth: () => h.auth }));
vi.mock('@/lib/useOrganizationTimezone', () => ({ useOrganizationTimezone: () => h.timezone }));
vi.mock('./call-dashboard.css', () => ({}));

import { CallDashboard } from './CallDashboard';

beforeAll(() => {
    // jsdom has no layout engine, resize observer or pointer capture; the charts, popover and select need them.
    globalThis.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
    Element.prototype.hasPointerCapture ??= () => false;
    Element.prototype.releasePointerCapture ??= () => {};
    Element.prototype.scrollIntoView ??= () => {};
});

function statsWith(total: number): CallStatsResponse {
    return {
        range: { start_date: '2026-09-14', end_date: '2026-09-20', timezone: 'Asia/Kolkata' },
        kpis: {
            total_calls: total,
            inbound_calls: total,
            outbound_calls: 0,
            successful_calls: total,
            failed_calls: 0,
            success_rate: total ? 100 : null,
            avg_duration_seconds: total ? 60 : null,
            total_duration_seconds: total * 60,
            calls_with_ticket: 0,
            tickets_created: 0,
            calls_with_closed_ticket: 0,
            calls_with_open_ticket: 0,
            analysed_calls: 0,
        },
        capabilities: { tickets: false, analysis: false },
        outcomes: [],
        disconnections: [],
        sentiment: [],
        satisfaction: [],
        reasons: [],
        daily: [{ date: '2026-09-20', calls: total, successful: total }],
        hourly: Array.from({ length: 24 }, (_, hour) => ({ hour, calls: 0 })),
    };
}

// The most recent request the dashboard made, as the query it sent.
function lastQuery() {
    const calls = h.getStats.mock.calls;
    return calls[calls.length - 1][0].query;
}

function totalCallsValue(): string {
    const tile = screen.getByText('Total calls').closest('.rounded-xl') as HTMLElement;
    return tile.querySelector('p.text-3xl')?.textContent ?? '';
}

async function loaded() {
    await waitFor(() => expect(screen.getByText('Total calls')).toBeTruthy());
}

describe('CallDashboard', () => {
    beforeEach(() => {
        vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-09-20T10:00:00Z') });
        h.timezone = 'Asia/Kolkata';
        h.auth = { isAuthenticated: true, loading: false };
        h.getStats.mockReset();
        h.getStats.mockResolvedValue({ data: statsWith(5) });
        h.getAgents.mockReset();
        h.getAgents.mockResolvedValue({
            data: [
                { id: 5, name: 'Vectus Smart Care' },
                { id: 7, name: 'Think Gas Inbound' },
            ],
        });
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    describe('what it asks for', () => {
        it('starts on the past 7 days, counted in the organization timezone', async () => {
            render(<CallDashboard />);
            await loaded();

            expect(h.getStats).toHaveBeenCalledTimes(1);
            expect(lastQuery()).toEqual({
                start_date: '2026-09-14',
                end_date: '2026-09-20',
                timezone: 'Asia/Kolkata',
            });
            expect(totalCallsValue()).toBe('5');
        });

        it('sends nothing until the viewer is signed in and the timezone is known', async () => {
            h.auth = { isAuthenticated: false, loading: true };
            const { rerender } = render(<CallDashboard />);
            await act(async () => {});
            expect(h.getStats).not.toHaveBeenCalled();

            h.auth = { isAuthenticated: true, loading: false };
            h.timezone = null;
            rerender(<CallDashboard />);
            await act(async () => {});
            expect(h.getStats).not.toHaveBeenCalled();

            h.timezone = 'Asia/Kolkata';
            rerender(<CallDashboard />);
            await loaded();
            expect(h.getStats).toHaveBeenCalledTimes(1);
        });

        it('means the organization\'s day, not the viewer\'s, for "Today"', async () => {
            // 20:00 UTC on the 20th is already 01:30 on the 21st in Kolkata.
            vi.setSystemTime(new Date('2026-09-20T20:00:00Z'));
            render(<CallDashboard />);
            await loaded();

            fireEvent.click(screen.getByRole('button', { name: 'Today' }));

            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-09-21', end_date: '2026-09-21' }),
            );
        });
    });

    describe('date presets', () => {
        it('re-asks for each preset and marks the active one', async () => {
            render(<CallDashboard />);
            await loaded();
            const pressed = (name: string) => screen.getByRole('button', { name }).getAttribute('aria-pressed');
            expect(pressed('Past 7 days')).toBe('true');

            fireEvent.click(screen.getByRole('button', { name: 'Today' }));
            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-09-20', end_date: '2026-09-20' }),
            );
            expect(pressed('Today')).toBe('true');
            expect(pressed('Past 7 days')).toBe('false');

            fireEvent.click(screen.getByRole('button', { name: 'Past 30 days' }));
            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-08-22', end_date: '2026-09-20' }),
            );
            expect(pressed('Past 30 days')).toBe('true');
        });

        it('asks for a custom range once both days are picked, then closes the picker', async () => {
            render(<CallDashboard />);
            await loaded();
            h.getStats.mockClear();

            fireEvent.click(screen.getByRole('button', { name: /Custom/ }));
            const day = (iso: string) => document.querySelector(`[data-day="${iso}"] button`) as HTMLElement;
            await waitFor(() => expect(day('2026-09-01')).toBeTruthy());

            fireEvent.click(day('2026-09-01'));
            // One end picked: nothing is asked for yet.
            expect(h.getStats).not.toHaveBeenCalled();
            fireEvent.click(day('2026-09-10'));

            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-09-01', end_date: '2026-09-10' }),
            );
            expect(screen.getByRole('button', { name: /1 Sept – 10 Sept|1 Sep – 10 Sep/ })).toBeTruthy();
            await waitFor(() => expect(document.querySelector('[data-day="2026-09-01"]')).toBeNull());
        });

        it('does not let days after today be picked', async () => {
            render(<CallDashboard />);
            await loaded();

            fireEvent.click(screen.getByRole('button', { name: /Custom/ }));

            await waitFor(() => expect(document.querySelector('[data-day="2026-09-20"] button')).toBeTruthy());
            const future = document.querySelector('[data-day="2026-09-21"] button') as HTMLButtonElement | null;
            expect(future === null || future.disabled).toBe(true);
        });
    });

    describe('picking a custom range takes two clicks', () => {
        const day = (iso: string) => document.querySelector(`[data-day="${iso}"] button`) as HTMLElement;

        async function openPicker() {
            render(<CallDashboard />);
            await loaded();
            h.getStats.mockClear();
            fireEvent.click(screen.getByRole('button', { name: /Custom/ }));
            await waitFor(() => expect(day('2026-09-01')).toBeTruthy());
        }

        it('shows each day once: the neighbouring month\'s days are not repeated in the other panel', async () => {
            await openPicker();

            // With two months side by side, the trailing days of one month would otherwise
            // reappear, selectable and highlighted as part of a range, in the other panel's
            // last row. The library keeps an empty placeholder cell for each; none may show a
            // day or hold a button.
            const outside = Array.from(document.querySelectorAll('[data-outside="true"]'));
            expect(outside.length).toBeGreaterThan(0);
            const visible = outside.filter(
                (cell) => cell.querySelector('button') || (cell.textContent ?? '').trim() !== '',
            );
            expect(visible).toHaveLength(0);

            // And every real day is selectable exactly once.
            const buttonsFor = (iso: string) => document.querySelectorAll(`[data-day="${iso}"] button`).length;
            expect(buttonsFor('2026-09-30')).toBe(1);
            expect(buttonsFor('2026-10-01')).toBe(1);
        });

        it('does not apply anything, or close, after the first day', async () => {
            await openPicker();

            fireEvent.click(day('2026-09-01'));

            expect(h.getStats).not.toHaveBeenCalled();
            expect(day('2026-09-10')).toBeTruthy(); // the picker is still open
            expect(screen.getByText('Now pick the last day.')).toBeTruthy();
        });

        it('accepts the two days in either order', async () => {
            await openPicker();

            fireEvent.click(day('2026-09-10'));
            fireEvent.click(day('2026-09-03'));

            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-09-03', end_date: '2026-09-10' }),
            );
        });

        it('gives a single-day range when the same day is picked twice', async () => {
            await openPicker();

            fireEvent.click(day('2026-09-05'));
            fireEvent.click(day('2026-09-05'));

            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-09-05', end_date: '2026-09-05' }),
            );
        });

        it('abandons a half-made pick when the picker is closed', async () => {
            await openPicker();
            fireEvent.click(day('2026-09-01'));

            fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
            await waitFor(() => expect(document.querySelector('[data-day="2026-09-01"]')).toBeNull());
            fireEvent.click(screen.getByRole('button', { name: /Custom/ }));
            await waitFor(() => expect(day('2026-09-02')).toBeTruthy());

            // A fresh pick starts over: this click is a first click again, not the end of the old one.
            fireEvent.click(day('2026-09-08'));
            expect(h.getStats).not.toHaveBeenCalled();
        });

        it('can start a new range after one has been chosen', async () => {
            await openPicker();
            fireEvent.click(day('2026-09-01'));
            fireEvent.click(day('2026-09-04'));
            await waitFor(() => expect(lastQuery()).toMatchObject({ end_date: '2026-09-04' }));

            fireEvent.click(screen.getByRole('button', { name: /Custom|Sep/ }));
            await waitFor(() => expect(day('2026-09-10')).toBeTruthy());
            fireEvent.click(day('2026-09-10'));
            fireEvent.click(day('2026-09-12'));

            await waitFor(() =>
                expect(lastQuery()).toMatchObject({ start_date: '2026-09-10', end_date: '2026-09-12' }),
            );
        });
    });

    describe('filters', () => {
        async function choose(selectName: string, optionName: string) {
            const trigger = screen.getByRole('combobox', { name: selectName });
            fireEvent.keyDown(trigger, { key: 'ArrowDown' });
            const option = await screen.findByRole('option', { name: optionName });
            fireEvent.keyDown(option, { key: 'Enter' });
        }

        it('filters to one agent, from the organization\'s own agents', async () => {
            render(<CallDashboard />);
            await loaded();

            await choose('Agent', 'Think Gas Inbound');

            await waitFor(() => expect(lastQuery()).toMatchObject({ workflow_id: 7 }));
        });

        it('filters to inbound or outbound calls, and back to all', async () => {
            render(<CallDashboard />);
            await loaded();

            await choose('Call type', 'Inbound');
            await waitFor(() => expect(lastQuery()).toMatchObject({ call_type: 'inbound' }));

            await choose('Call type', 'All calls');
            await waitFor(() => expect(lastQuery()).not.toHaveProperty('call_type'));
        });
    });

    describe('test calls', () => {
        it('are counted only when an admin switches them on', async () => {
            render(<CallDashboard showTestCallsSwitch />);
            await loaded();
            expect(lastQuery()).not.toHaveProperty('include_test_calls');
            expect(screen.getByText(/Browser test sessions are not counted/)).toBeTruthy();

            fireEvent.click(screen.getByRole('switch', { name: 'Include test calls' }));

            await waitFor(() => expect(lastQuery()).toMatchObject({ include_test_calls: true }));
            expect(screen.getByText(/Browser test sessions are included/)).toBeTruthy();

            fireEvent.click(screen.getByRole('switch', { name: 'Include test calls' }));
            await waitFor(() => expect(lastQuery()).not.toHaveProperty('include_test_calls'));
        });

        it('cannot be switched on by a client: there is no switch and the request never asks', async () => {
            render(<CallDashboard />);
            await loaded();

            expect(screen.queryByRole('switch')).toBeNull();
            expect(screen.queryByText('Include test calls')).toBeNull();
            expect(lastQuery()).not.toHaveProperty('include_test_calls');
        });
    });

    describe('while the numbers change', () => {
        it('keeps the previous numbers on screen, dimmed, until the new ones arrive', async () => {
            render(<CallDashboard />);
            await loaded();

            let release: (value: unknown) => void = () => {};
            h.getStats.mockReturnValueOnce(new Promise((resolve) => (release = resolve)));
            fireEvent.click(screen.getByRole('button', { name: 'Today' }));

            await waitFor(() => expect(document.querySelector('.cd-root')?.getAttribute('aria-busy')).toBe('true'));
            expect(totalCallsValue()).toBe('5'); // still the old figure, not a skeleton

            await act(async () => release({ data: statsWith(2) }));
            await waitFor(() => expect(totalCallsValue()).toBe('2'));
            expect(document.querySelector('.cd-root')?.getAttribute('aria-busy')).toBe('false');
        });

        it('ignores a slow answer that arrives after a newer request was made', async () => {
            render(<CallDashboard />);
            await loaded();

            // The 30-day answer is slow; the viewer moves on to Today, which is quick.
            let releaseSlow: (value: unknown) => void = () => {};
            h.getStats.mockReturnValueOnce(new Promise((resolve) => (releaseSlow = resolve)));
            fireEvent.click(screen.getByRole('button', { name: 'Past 30 days' }));
            h.getStats.mockResolvedValueOnce({ data: statsWith(2) });
            fireEvent.click(screen.getByRole('button', { name: 'Today' }));
            await waitFor(() => expect(totalCallsValue()).toBe('2'));

            await act(async () => releaseSlow({ data: statsWith(99) }));

            expect(totalCallsValue()).toBe('2');
        });
    });

    describe('when it cannot load', () => {
        it('says so, in words, if the request fails', async () => {
            h.getStats.mockResolvedValue({ error: { detail: 'boom' } });
            render(<CallDashboard />);

            const alert = await screen.findByRole('alert');
            expect(alert.textContent).toBe('The dashboard could not be loaded. Please try again.');
        });

        it('says so if the request throws', async () => {
            h.getStats.mockRejectedValue(new Error('network'));
            render(<CallDashboard />);

            expect((await screen.findByRole('alert')).textContent).toContain('could not be loaded');
        });

        it('recovers on the next successful request and clears the message', async () => {
            h.getStats.mockResolvedValueOnce({ error: { detail: 'boom' } });
            render(<CallDashboard />);
            await screen.findByRole('alert');

            h.getStats.mockResolvedValue({ data: statsWith(4) });
            fireEvent.click(screen.getByRole('button', { name: 'Today' }));

            await waitFor(() => expect(totalCallsValue()).toBe('4'));
            expect(screen.queryByRole('alert')).toBeNull();
        });

        it('still shows the dashboard if the agent list cannot be loaded', async () => {
            h.getAgents.mockRejectedValue(new Error('no agents'));
            const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
            render(<CallDashboard />);

            await loaded();
            expect(totalCallsValue()).toBe('5');
            consoleError.mockRestore();
        });
    });
});
