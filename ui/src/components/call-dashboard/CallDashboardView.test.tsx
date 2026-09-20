import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import type { CallStatsResponse } from '@/client/types.gen';

import { CallDashboardView } from './CallDashboardView';

// The stylesheet only defines chart colours; the project's PostCSS setup is not needed to test behaviour.
vi.mock('./call-dashboard.css', () => ({}));

// jsdom has no layout engine; the responsive chart container needs a resize observer to mount.
beforeAll(() => {
    globalThis.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
});

const hourly = Array.from({ length: 24 }, (_, hour) => ({ hour, calls: hour === 9 ? 3 : hour === 14 ? 1 : 0 }));

const stats: CallStatsResponse = {
    range: { start_date: '2026-09-01', end_date: '2026-09-02', timezone: 'Asia/Kolkata' },
    kpis: {
        total_calls: 4,
        inbound_calls: 3,
        outbound_calls: 1,
        successful_calls: 2,
        failed_calls: 1,
        success_rate: 50,
        avg_duration_seconds: 69,
        total_duration_seconds: 276,
        calls_with_ticket: 2,
        tickets_created: 2,
        calls_with_closed_ticket: 1,
        calls_with_open_ticket: 1,
        analysed_calls: 2,
    },
    capabilities: { tickets: true, analysis: true },
    outcomes: [
        { key: 'ticket_closed', label: 'Ticket created and closed', count: 1 },
        { key: 'failed', label: 'Call failed', count: 1 },
    ],
    disconnections: [{ key: 'customer_hung_up', label: 'Customer hung up', count: 3 }],
    sentiment: [
        { key: 'positive', label: 'Positive', count: 1 },
        { key: 'neutral', label: 'Neutral', count: 1 },
    ],
    satisfaction: [
        { key: 'yes', label: 'Yes', count: 1 },
        { key: 'no', label: 'No', count: 0 },
        { key: 'unclear', label: 'Unclear', count: 1 },
    ],
    reasons: [
        { key: 'recharge', label: 'Recharge', count: 1 },
        { key: 'meter_balance', label: 'Meter balance', count: 1 },
    ],
    daily: [
        { date: '2026-09-01', calls: 1, successful: 1 },
        { date: '2026-09-02', calls: 3, successful: 1 },
    ],
    hourly,
};

// The card that contains a given piece of text (the project's Card has no data attributes to hook onto).
function cardOf(text: string) {
    return screen.getByText(text).closest('.rounded-xl') as HTMLElement;
}

function tile(label: string) {
    return cardOf(label);
}

describe('CallDashboardView', () => {
    it('shows the headline numbers with what they count', () => {
        render(<CallDashboardView stats={stats} />);

        expect(within(tile('Total calls')).getByText('4')).toBeTruthy();
        expect(within(tile('Total calls')).getByText('3 inbound · 1 outbound')).toBeTruthy();
        expect(within(tile('Success rate')).getByText('50%')).toBeTruthy();
        expect(within(tile('Success rate')).getByText('2 of 4 calls completed')).toBeTruthy();
        expect(within(tile('Average duration')).getByText('1m 9s')).toBeTruthy();
        expect(within(tile('Tickets created')).getByText('2')).toBeTruthy();
        expect(within(tile('Tickets created')).getByText('1 calls closed · 1 still open')).toBeTruthy();
        expect(within(tile('Failed calls')).getByText('1')).toBeTruthy();
    });

    it('renders every chart card', () => {
        render(<CallDashboardView stats={stats} />);

        for (const title of [
            'Calls per day',
            'Calls by hour',
            'Call outcomes',
            'Reason for call',
            'Customer sentiment',
            'Customer satisfied',
            'How calls ended',
        ]) {
            expect(screen.getByText(title)).toBeTruthy();
        }
    });

    it('names every diverging segment with its count and share, not by colour alone', () => {
        render(<CallDashboardView stats={stats} />);

        const sentiment = cardOf('Customer sentiment');
        expect(within(sentiment).getByText('Positive')).toBeTruthy();
        expect(within(sentiment).getByText('Negative')).toBeTruthy();
        expect(within(sentiment).getAllByText('1 · 50%').length).toBe(2);
        expect(within(sentiment).getByText('0 · 0%')).toBeTruthy();
    });

    it('offers every chart as a table with the same numbers', () => {
        render(<CallDashboardView stats={stats} />);

        const outcomes = cardOf('Call outcomes');
        fireEvent.click(within(outcomes).getByRole('button', { name: 'table' }));

        expect(within(outcomes).getByText('Outcome')).toBeTruthy();
        expect(within(outcomes).getByText('Ticket created and closed')).toBeTruthy();
        expect(within(outcomes).getAllByText('50%').length).toBe(2);
        expect(within(outcomes).getByRole('button', { name: 'table' }).getAttribute('aria-pressed')).toBe('true');
    });

    it('says how many calls the QA-based charts rest on', () => {
        render(<CallDashboardView stats={stats} />);

        expect(screen.getAllByText('Based on 2 of 4 calls analysed').length).toBe(3);
    });

    it('is honest when QA analysed nothing', () => {
        render(
            <CallDashboardView
                stats={{
                    ...stats,
                    kpis: { ...stats.kpis, analysed_calls: 0 },
                    sentiment: [],
                    satisfaction: [],
                    reasons: [],
                }}
            />,
        );

        expect(screen.getAllByText('No calls were analysed in this period').length).toBe(3);
        expect(screen.getAllByText('No analysed calls in this period').length).toBe(3);
    });

    it('shows an empty state instead of empty charts when there were no calls', () => {
        render(
            <CallDashboardView
                stats={{
                    ...stats,
                    kpis: {
                        ...stats.kpis,
                        total_calls: 0,
                        success_rate: null,
                        avg_duration_seconds: null,
                        tickets_created: 0,
                    },
                }}
            />,
        );

        expect(screen.getByText('No calls in this period')).toBeTruthy();
        expect(screen.queryByText('Calls per day')).toBeNull();
        expect(within(tile('Success rate')).getByText('—')).toBeTruthy();
    });

    it('keeps the previous numbers on screen, dimmed, while refreshing', () => {
        const { container } = render(<CallDashboardView stats={stats} refreshing />);

        const root = container.querySelector('.cd-root') as HTMLElement;
        expect(root.getAttribute('aria-busy')).toBe('true');
        expect(root.className).toContain('opacity-60');
        expect(within(tile('Total calls')).getByText('4')).toBeTruthy();
    });

    it('shows an error when there is nothing to show', () => {
        render(<CallDashboardView stats={null} error="The dashboard could not be loaded." />);

        expect(screen.getByRole('alert').textContent).toBe('The dashboard could not be loaded.');
    });
});

describe('CallDashboardView for agents that cannot produce everything', () => {
    const QA_CHARTS = ['Reason for call', 'Customer sentiment', 'Customer satisfied'];

    it('leaves out tickets and QA-based charts when no agent in scope can produce them', () => {
        render(<CallDashboardView stats={{ ...stats, capabilities: { tickets: false, analysis: false } }} />);

        expect(screen.queryByText('Tickets created')).toBeNull();
        expect(screen.queryByText('Call outcomes')).toBeNull();
        for (const title of QA_CHARTS) expect(screen.queryByText(title)).toBeNull();
        // Everything that does not depend on them is still there.
        for (const title of ['Total calls', 'Success rate', 'Average duration', 'Failed calls']) {
            expect(screen.getByText(title)).toBeTruthy();
        }
        for (const title of ['Calls per day', 'Calls by hour', 'How calls ended']) {
            expect(screen.getByText(title)).toBeTruthy();
        }
    });

    it('keeps tickets and outcomes for a ticket agent that has no QA', () => {
        render(<CallDashboardView stats={{ ...stats, capabilities: { tickets: true, analysis: false } }} />);

        expect(screen.getByText('Tickets created')).toBeTruthy();
        expect(screen.getByText('Call outcomes')).toBeTruthy();
        for (const title of QA_CHARTS) expect(screen.queryByText(title)).toBeNull();
    });

    it('keeps the QA charts for an agent with QA but no tickets', () => {
        render(<CallDashboardView stats={{ ...stats, capabilities: { tickets: false, analysis: true } }} />);

        expect(screen.queryByText('Tickets created')).toBeNull();
        expect(screen.getByText('Call outcomes')).toBeTruthy();
        for (const title of QA_CHARTS) expect(screen.getByText(title)).toBeTruthy();
    });

    it('shows everything when the response carries no capability information', () => {
        const { capabilities, ...withoutCapabilities } = stats;
        void capabilities;

        render(<CallDashboardView stats={withoutCapabilities as CallStatsResponse} />);

        expect(screen.getByText('Tickets created')).toBeTruthy();
        for (const title of QA_CHARTS) expect(screen.getByText(title)).toBeTruthy();
    });
});
