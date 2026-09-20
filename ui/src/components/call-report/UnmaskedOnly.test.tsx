import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { UnmaskedOnly } from './UnmaskedOnly';

function renderGate(phoneMasked: boolean | null) {
    return render(
        <UnmaskedOnly phoneMasked={phoneMasked}>
            <p>caller_number: +919876543210</p>
        </UnmaskedOnly>,
    );
}

describe('UnmaskedOnly', () => {
    it('shows the raw context only when the number is known not to be masked', () => {
        renderGate(false);

        expect(screen.getByText('caller_number: +919876543210')).toBeTruthy();
    });

    it('hides it when the number was masked for this viewer', () => {
        renderGate(true);

        expect(screen.queryByText(/caller_number/)).toBeNull();
    });

    it('hides it until the report has loaded, so it never flashes up before masking is known', () => {
        renderGate(null);

        expect(screen.queryByText(/caller_number/)).toBeNull();
    });

    it('stays hidden if the report never loads (a failed request leaves the state unknown)', () => {
        const { rerender } = renderGate(null);
        rerender(
            <UnmaskedOnly phoneMasked={null}>
                <p>caller_number: +919876543210</p>
            </UnmaskedOnly>,
        );

        expect(screen.queryByText(/caller_number/)).toBeNull();
    });

    it('closes again if masking becomes known after the content was shown', () => {
        const { rerender } = renderGate(false);
        expect(screen.getByText(/caller_number/)).toBeTruthy();

        rerender(
            <UnmaskedOnly phoneMasked>
                <p>caller_number: +919876543210</p>
            </UnmaskedOnly>,
        );

        expect(screen.queryByText(/caller_number/)).toBeNull();
    });
});
