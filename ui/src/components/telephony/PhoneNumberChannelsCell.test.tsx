import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ update: vi.fn(), failWith: null as string | null }));

vi.mock('@/lib/superadminPhoneNumbers', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/lib/superadminPhoneNumbers')>();
    return {
        ...actual,
        updatePhoneNumberChannels: async (...args: unknown[]) => {
            const result = h.update(...args);
            if (h.failWith) throw new Error(h.failWith);
            return result;
        },
    };
});
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { toast } from 'sonner';

import type { PhoneNumberWithChannels } from '@/lib/superadminPhoneNumbers';

import { PhoneNumberChannelsCell } from './PhoneNumberChannelsCell';

function number(channels?: number): PhoneNumberWithChannels {
    return {
        id: 7,
        telephony_configuration_id: 3,
        address: '+919429396634',
        address_normalized: '+919429396634',
        address_type: 'pstn',
        is_active: true,
        is_default_caller_id: true,
        extra_metadata: {},
        created_at: '2026-09-28T00:00:00Z',
        updated_at: '2026-09-28T00:00:00Z',
        ...(channels === undefined ? {} : { max_concurrent_calls: channels }),
    } as PhoneNumberWithChannels;
}

beforeEach(() => {
    h.update.mockReset();
    h.failWith = null;
});

describe('PhoneNumberChannelsCell', () => {
    it('shows the channel count to everyone, with no edit control for org members', () => {
        render(<PhoneNumberChannelsCell phoneNumber={number(5)} canEdit={false} onSaved={vi.fn()} />);
        expect(screen.getByText('5')).toBeTruthy();
        expect(screen.queryByRole('button', { name: /change channels/i })).toBeNull();
    });

    it('treats a number from before channels existed as 1 channel', () => {
        render(<PhoneNumberChannelsCell phoneNumber={number()} canEdit={false} onSaved={vi.fn()} />);
        expect(screen.getByText('1')).toBeTruthy();
    });

    it('lets a superadmin save a new value', async () => {
        const onSaved = vi.fn();
        h.update.mockResolvedValue(number(10));
        render(<PhoneNumberChannelsCell phoneNumber={number(1)} canEdit onSaved={onSaved} />);

        fireEvent.click(screen.getByRole('button', { name: /change channels/i }));
        fireEvent.change(screen.getByLabelText(/channels for/i), { target: { value: '10' } });
        fireEvent.click(screen.getByRole('button', { name: /save channels/i }));

        await waitFor(() => expect(onSaved).toHaveBeenCalledWith(number(10)));
        expect(h.update).toHaveBeenCalledWith(7, 10);
    });

    it.each(['0', '201', '2.5', ''])('refuses an invalid value (%s) without calling the API', (bad) => {
        render(<PhoneNumberChannelsCell phoneNumber={number(1)} canEdit onSaved={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /change channels/i }));
        fireEvent.change(screen.getByLabelText(/channels for/i), { target: { value: bad } });

        const save = screen.getByRole('button', { name: /save channels/i }) as HTMLButtonElement;
        expect(save.disabled).toBe(true);
        fireEvent.keyDown(screen.getByLabelText(/channels for/i), { key: 'Enter' });
        expect(h.update).not.toHaveBeenCalled();
    });

    it('does not call the API when the value is unchanged', () => {
        render(<PhoneNumberChannelsCell phoneNumber={number(4)} canEdit onSaved={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /change channels/i }));
        fireEvent.click(screen.getByRole('button', { name: /save channels/i }));
        expect(h.update).not.toHaveBeenCalled();
    });

    it('keeps the old value and stays in edit mode when saving fails', async () => {
        const onSaved = vi.fn();
        h.failWith = 'Not a superuser';
        render(<PhoneNumberChannelsCell phoneNumber={number(2)} canEdit onSaved={onSaved} />);

        fireEvent.click(screen.getByRole('button', { name: /change channels/i }));
        fireEvent.change(screen.getByLabelText(/channels for/i), { target: { value: '8' } });
        fireEvent.click(screen.getByRole('button', { name: /save channels/i }));

        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Not a superuser'));
        expect(onSaved).not.toHaveBeenCalled();
        expect(screen.getByLabelText(/channels for/i)).toBeTruthy();
    });
});
