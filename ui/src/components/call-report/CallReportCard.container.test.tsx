import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { CallReportResponse } from '@/client/types.gen';

const h = vi.hoisted(() => ({
    auth: { isAuthenticated: true, loading: false },
    getReport: vi.fn(),
    timezone: 'Asia/Kolkata' as string | null,
}));

vi.mock('@/client/sdk.gen', () => ({
    getRunCallReportApiV1WorkflowWorkflowIdRunsRunIdCallReportGet: (...args: unknown[]) => h.getReport(...args),
}));
vi.mock('@/lib/auth', () => ({ useAuth: () => h.auth }));
vi.mock('@/lib/useOrganizationTimezone', () => ({ useOrganizationTimezone: () => h.timezone }));

import { CallReportCard } from './CallReportCard';

function report(overrides: Partial<CallReportResponse> = {}): CallReportResponse {
    return {
        schema_version: 1,
        run_id: 439,
        workflow_id: 5,
        organization_id: 1,
        call: {
            call_type: 'inbound',
            mode: 'voicelink',
            is_telephony: true,
            started_at: '2026-09-17T09:37:52.783+00:00',
            duration_seconds: 251,
            phone_number: '+91 98••••3210',
        },
        disconnect: { reason: 'user_hangup', category: 'customer_hung_up', label: 'Customer hung up' },
        outcome: { code: 'no_outcome', label: 'No outcome recorded', successful: true },
        ticket: { created: false, closed: false, count: 0, tickets: [] },
        analysis: { status: 'not_run', tags: [], nodes: [] },
        capabilities: { ticket: false, analysis: false },
        captured: {},
        phone_masked: true,
        ...overrides,
    };
}

describe('CallReportCard', () => {
    beforeEach(() => {
        h.auth = { isAuthenticated: true, loading: false };
        h.timezone = 'Asia/Kolkata';
        h.getReport.mockReset();
    });

    it('asks for the right run and shows the report', async () => {
        h.getReport.mockResolvedValue({ data: report() });
        render(<CallReportCard workflowId={5} runId={439} />);

        await waitFor(() => expect(screen.getByText('+91 98••••3210')).toBeTruthy());
        expect(h.getReport).toHaveBeenCalledWith({ path: { workflow_id: 5, run_id: 439 } });
        expect(screen.getByText('Times shown in Asia/Kolkata')).toBeTruthy();
    });

    it('tells the page whether the number was masked, once the report has loaded', async () => {
        h.getReport.mockResolvedValue({ data: report({ phone_masked: true }) });
        const onLoaded = vi.fn();
        render(<CallReportCard workflowId={5} runId={439} onLoaded={onLoaded} />);

        await waitFor(() => expect(onLoaded).toHaveBeenCalledTimes(1));
        expect(onLoaded.mock.calls[0][0].phone_masked).toBe(true);
    });

    it('never tells the page anything if the report cannot be loaded, so the page keeps raw data hidden', async () => {
        h.getReport.mockResolvedValue({ error: { detail: 'not found' } });
        const onLoaded = vi.fn();
        render(<CallReportCard workflowId={5} runId={439} onLoaded={onLoaded} />);

        await waitFor(() => expect(screen.getByText('The call report could not be loaded.')).toBeTruthy());
        expect(onLoaded).not.toHaveBeenCalled();
    });

    it('treats a thrown error the same way', async () => {
        h.getReport.mockRejectedValue(new Error('network'));
        const onLoaded = vi.fn();
        render(<CallReportCard workflowId={5} runId={439} onLoaded={onLoaded} />);

        await waitFor(() => expect(screen.getByText('The call report could not be loaded.')).toBeTruthy());
        expect(onLoaded).not.toHaveBeenCalled();
    });

    it('does not ask until the viewer is signed in', async () => {
        h.auth = { isAuthenticated: false, loading: true };
        h.getReport.mockResolvedValue({ data: report() });
        render(<CallReportCard workflowId={5} runId={439} />);

        await new Promise((resolve) => setTimeout(resolve, 20));
        expect(h.getReport).not.toHaveBeenCalled();
    });

    it('asks again for a different run rather than showing the old one', async () => {
        h.getReport.mockResolvedValueOnce({ data: report({ run_id: 439 }) });
        const { rerender } = render(<CallReportCard workflowId={5} runId={439} />);
        await waitFor(() => expect(h.getReport).toHaveBeenCalledTimes(1));

        h.getReport.mockResolvedValueOnce({ data: report({ run_id: 440, call: { ...report().call, phone_number: '+91 11••••2222' } }) });
        rerender(<CallReportCard workflowId={5} runId={440} />);

        await waitFor(() => expect(screen.getByText('+91 11••••2222')).toBeTruthy());
        expect(h.getReport).toHaveBeenLastCalledWith({ path: { workflow_id: 5, run_id: 440 } });
    });

    it('offers the same report as JSON, exactly as served (masked number included)', async () => {
        h.getReport.mockResolvedValue({ data: report() });
        render(<CallReportCard workflowId={5} runId={439} />);
        await waitFor(() => expect(screen.getByText('+91 98••••3210')).toBeTruthy());

        fireEvent.mouseDown(screen.getByRole('tab', { name: 'JSON' }));

        await waitFor(() => expect(document.body.textContent).toContain('"phone_number": "+91 98••••3210"'));
        expect(document.body.textContent).toContain('"phone_masked": true');
    });
});
