import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { parseQaResponse, QAResultsCard } from './QAResultsCard';

const node1 = JSON.stringify({
    tags: [],
    overall_sentiment: 'neutral',
    call_quality_score: 10,
    summary: 'The agent greeted the caller.',
    issue_type: 'recharge',
    ticket: { ticket_created: false, ticket_id: null, ticket_status: 'not_created' },
});

// The reviewer often wraps its answer in a markdown fence, as it did for run 413's second node.
const node2 =
    '```json\n' +
    JSON.stringify({
        tags: [{ tag: 'CALLER_NUMBER_LOOKUP_MISSED', reason: 'asked registered number first' }],
        overall_sentiment: 'neutral',
        summary: 'Created a complaint ticket and transferred to senior staff.',
        issue_type: 'meter_balance',
        human_transfer: true,
        ticket: { ticket_created: true, ticket_id: null, ticket_status: 'created_open' },
    }) +
    '\n```';

const annotations = {
    qa_5: {
        model: 'test-model',
        node_results: {
            '1': { node_name: 'start call', raw_response: node1 },
            '2': { node_name: 'Main Agenda and Questions', raw_response: node2 },
        },
    },
    tags: ['CALLER_NUMBER_LOOKUP_MISSED'],
};

describe('parseQaResponse', () => {
    it('reads plain JSON and fenced JSON', () => {
        expect(parseQaResponse('{"a": 1}')).toEqual({ a: 1 });
        expect(parseQaResponse('```json\n{"a": 1}\n```')).toEqual({ a: 1 });
        expect(parseQaResponse('```\n{"a": 1}\n```')).toEqual({ a: 1 });
    });

    it('returns null for anything that is not a JSON object', () => {
        expect(parseQaResponse('not json')).toBeNull();
        expect(parseQaResponse('[1, 2]')).toBeNull();
        expect(parseQaResponse(undefined)).toBeNull();
        expect(parseQaResponse('')).toBeNull();
    });
});

describe('QAResultsCard', () => {
    it('shows each node as readable fields, not raw text', () => {
        render(<QAResultsCard annotations={annotations} />);

        expect(screen.getByText('start call')).toBeTruthy();
        expect(screen.getByText('Main Agenda and Questions')).toBeTruthy();
        expect(screen.getByText('Created a complaint ticket and transferred to senior staff.')).toBeTruthy();
        // Keys become labels and booleans become Yes/No.
        expect(screen.getAllByText('Issue type').length).toBe(2);
        expect(screen.getByText('meter_balance')).toBeTruthy();
        expect(screen.getByText('Human transfer')).toBeTruthy();
        // Node 2 says both "human transfer" and "ticket created" are true.
        expect(screen.getAllByText('Yes').length).toBe(2);
        // Nothing here shows raw braces.
        expect(screen.queryByText(/"issue_type"/)).toBeNull();
    });

    it('shows QA tags as badges with their reasons', () => {
        render(<QAResultsCard annotations={annotations} />);

        // Once inside its node (with the reason) and once in the run's top-level tag list.
        expect(screen.getAllByText('CALLER_NUMBER_LOOKUP_MISSED').length).toBe(2);
        expect(screen.getByText('asked registered number first')).toBeTruthy();
    });

    it('swaps to the raw JSON, exactly as stored, and back', () => {
        render(<QAResultsCard annotations={annotations} />);

        const formatted = screen.getByRole('tab', { name: 'Formatted' });
        const json = screen.getByRole('tab', { name: 'JSON' });
        expect(formatted.getAttribute('data-state')).toBe('active');

        // Radix tabs switch on mouse down.
        fireEvent.mouseDown(json);
        expect(json.getAttribute('data-state')).toBe('active');
        expect(document.body.textContent).toContain('"node_name": "Main Agenda and Questions"');
        expect(document.body.textContent).toContain('"model": "test-model"');

        fireEvent.mouseDown(formatted);
        expect(formatted.getAttribute('data-state')).toBe('active');
    });

    it('falls back to the stored fields and the raw text when the response is not JSON', () => {
        render(
            <QAResultsCard
                annotations={{
                    qa_5: {
                        node_results: {
                            '1': {
                                node_name: 'start call',
                                raw_response: 'Sorry, I cannot do that.',
                                summary: 'stored summary',
                                overall_sentiment: 'negative',
                                score: 4,
                                tags: [],
                            },
                        },
                    },
                }}
            />,
        );

        expect(screen.getByText('stored summary')).toBeTruthy();
        expect(screen.getByText('negative')).toBeTruthy();
        expect(screen.getByText('Sorry, I cannot do that.')).toBeTruthy();
    });

    it('explains a skipped or failed QA run instead of showing nothing', () => {
        const { rerender } = render(
            <QAResultsCard annotations={{ qa_5: { skipped: true, reason: 'call too short' } }} />,
        );
        expect(screen.getByText('QA analysis was skipped: call too short.')).toBeTruthy();

        rerender(<QAResultsCard annotations={{ qa_5: { error: 'no_api_key', node_results: {} } }} />);
        expect(screen.getByText('QA analysis failed: no_api_key')).toBeTruthy();
    });
});
