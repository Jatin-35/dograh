import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { CallReportResponse } from '@/client/types.gen';

import { ReportView } from './CallReportCard';

// The card fetches through the API client and auth context; the readable report is what is under test.
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ isAuthenticated: false, loading: true }) }));
vi.mock('@/client/sdk.gen', () => ({
    getRunCallReportApiV1WorkflowWorkflowIdRunsRunIdCallReportGet: vi.fn(),
    getPreferencesApiV1OrganizationsPreferencesGet: vi.fn(),
}));

const IST = 'Asia/Kolkata';

// A browser test of an outbound sales agent: no tickets, no QA node, but it captures data.
const salesWebCall: CallReportResponse = {
    schema_version: 1,
    run_id: 439,
    workflow_id: 5,
    organization_id: 1,
    call: {
        call_type: 'outbound',
        mode: 'smallwebrtc',
        is_telephony: false,
        started_at: '2026-09-17T09:37:52.783+00:00',
        duration_seconds: 251,
        phone_number: null,
    },
    disconnect: { reason: 'user_qualified', category: 'agent_completed', label: 'Agent completed the flow' },
    outcome: { code: 'no_outcome', label: 'No outcome recorded', successful: true },
    ticket: { created: false, closed: false, count: 0, tickets: [] },
    analysis: { status: 'not_run', tags: [], nodes: [] },
    capabilities: { ticket: false, analysis: false },
    captured: { interested_item: 'water tank', product_category: 'Water Tank', conversation_language: 'hi' },
};

// An inbound support call on an agent that has a ticket tool and a QA node.
const supportCall: CallReportResponse = {
    schema_version: 1,
    run_id: 413,
    workflow_id: 7,
    organization_id: 5,
    call: {
        call_type: 'inbound',
        mode: 'voicelink',
        is_telephony: true,
        started_at: '2026-09-17T09:34:18+00:00',
        duration_seconds: 214,
        phone_number: '+91 12••••7890',
    },
    disconnect: { reason: 'user_hangup', category: 'customer_hung_up', label: 'Customer hung up' },
    outcome: { code: 'ticket_open_transferred', label: 'Ticket open, transferred to a person', successful: true },
    ticket: {
        created: true,
        closed: false,
        count: 1,
        tickets: [
            {
                ticket_id: '7000002403',
                created_at: '2026-09-17T09:37:52.783+00:00',
                closed: false,
                source: 'think_gas_sap',
            },
        ],
    },
    analysis: {
        status: 'analysed',
        sentiment: 'neutral',
        reason_for_call: 'meter_balance',
        resolved: false,
        human_transfer: true,
        summary: 'Created a complaint ticket and transferred to senior staff.',
        tags: [],
        nodes: [],
    },
    capabilities: { ticket: true, analysis: true },
    captured: {},
    phone_masked: true,
};

describe('ReportView for an agent without tickets or QA', () => {
    it('shows only what applies: no ticket, analysis or outcome sections', () => {
        render(<ReportView report={salesWebCall} timezone={IST} />);

        expect(screen.queryByText('Ticket')).toBeNull();
        expect(screen.queryByText('Call analysis')).toBeNull();
        expect(screen.queryByText('Outcome')).toBeNull();
        expect(screen.queryByText('No outcome recorded')).toBeNull();
        expect(screen.queryByText('Not analysed')).toBeNull();
    });

    it('labels a browser session as a web test call instead of leaving dashes', () => {
        render(<ReportView report={salesWebCall} timezone={IST} />);

        expect(screen.getByText('Web test call')).toBeTruthy();
        expect(screen.getByText('Web call, no number')).toBeTruthy();
        expect(screen.queryByText('Outbound')).toBeNull();
    });

    it('shows what the agent captured, under the agent\'s own names', () => {
        render(<ReportView report={salesWebCall} timezone={IST} />);

        expect(screen.getByText('Captured data')).toBeTruthy();
        expect(screen.getByText('Interested item')).toBeTruthy();
        expect(screen.getByText('water tank')).toBeTruthy();
        expect(screen.getByText('Product category')).toBeTruthy();
        expect(screen.getByText('Water Tank')).toBeTruthy();
        expect(screen.getByText('Conversation language')).toBeTruthy();
    });

    it('shows the time in the organization timezone on a 12-hour clock', () => {
        render(<ReportView report={salesWebCall} timezone={IST} />);

        // 09:37:52 UTC is 3:07:52 PM in Kolkata.
        expect(screen.getByText('17 Sep 2026, 3:07:52 PM')).toBeTruthy();
        expect(screen.getByText('4m 11s')).toBeTruthy();
    });
});

describe('ReportView for an agent with tickets and QA', () => {
    it('shows the ticket and analysis sections with times in the organization timezone', () => {
        render(<ReportView report={supportCall} timezone={IST} />);

        expect(screen.getByText('Ticket')).toBeTruthy();
        expect(screen.getByText('7000002403')).toBeTruthy();
        // The ticket was created at 09:37:52 UTC.
        expect(screen.getAllByText('17 Sep 2026, 3:07:52 PM').length).toBe(1);
        // The call started at 09:34:18 UTC.
        expect(screen.getByText('17 Sep 2026, 3:04:18 PM')).toBeTruthy();

        expect(screen.getByText('Call analysis')).toBeTruthy();
        expect(screen.getByText('Meter balance')).toBeTruthy();
        expect(screen.getByText('Created a complaint ticket and transferred to senior staff.')).toBeTruthy();
        expect(screen.getByText('Ticket open, transferred to a person')).toBeTruthy();
    });

    it('shows a real call as its type, with the number as served (masked)', () => {
        render(<ReportView report={supportCall} timezone={IST} />);

        expect(screen.getByText('Inbound')).toBeTruthy();
        expect(screen.getByText('+91 12••••7890')).toBeTruthy();
        expect(screen.queryByText('Web test call')).toBeNull();
    });

    it('does not show a captured-data section when nothing was captured', () => {
        render(<ReportView report={supportCall} timezone={IST} />);

        expect(screen.queryByText('Captured data')).toBeNull();
    });

    it('keeps the sections for an agent that can raise tickets even on a call that raised none', () => {
        render(
            <ReportView
                report={{
                    ...supportCall,
                    ticket: { created: false, closed: false, count: 0, tickets: [] },
                    analysis: { status: 'not_run', tags: [], nodes: [] },
                    outcome: { code: 'no_outcome', label: 'No outcome recorded', successful: false },
                }}
                timezone={IST}
            />,
        );

        const ticket = screen.getByText('Ticket').closest('section') as HTMLElement;
        expect(within(ticket).getByText('Created')).toBeTruthy();
        expect(within(ticket).getByText('No')).toBeTruthy();
        expect(screen.getByText('Not analysed')).toBeTruthy();
        expect(screen.getByText('No outcome recorded')).toBeTruthy();
    });

    it('explains a skipped analysis', () => {
        render(
            <ReportView
                report={{
                    ...supportCall,
                    analysis: { status: 'skipped', skipped_reason: 'call too short', tags: [], nodes: [] },
                }}
                timezone={IST}
            />,
        );

        expect(screen.getByText('Skipped')).toBeTruthy();
        expect(screen.getByText('Call analysis was skipped: call too short.')).toBeTruthy();
    });
});

describe('ReportView proves capability from the call itself', () => {
    it('shows the ticket section when a ticket exists even if the report says the agent has no ticket tool', () => {
        render(
            <ReportView report={{ ...supportCall, capabilities: { ticket: false, analysis: false } }} timezone={IST} />,
        );

        expect(screen.getByText('Ticket')).toBeTruthy();
        expect(screen.getByText('7000002403')).toBeTruthy();
    });

    it('treats a report with no capability information by what it contains', () => {
        const { capabilities, ...withoutCapabilities } = salesWebCall;
        void capabilities;

        render(<ReportView report={withoutCapabilities} timezone={IST} />);

        expect(screen.queryByText('Ticket')).toBeNull();
        expect(screen.queryByText('Call analysis')).toBeNull();
    });
});
