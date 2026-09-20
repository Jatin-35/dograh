import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getAuthUser = vi.fn();

vi.mock('@/client/sdk.gen', () => ({
    getAuthUserApiV1UserAuthUserGet: (...args: unknown[]) => getAuthUser(...args),
}));
vi.mock('@/lib/auth', () => ({
    useAuth: () => ({
        user: { displayName: 'Priya Sharma' },
        loading: false,
        getAccessToken: async () => 'token',
    }),
}));
vi.mock('next/link', () => ({
    default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
        <a href={href} {...rest}>
            {children}
        </a>
    ),
}));
vi.mock('@/components/call-dashboard/CallDashboard', () => ({
    CallDashboard: ({ showTestCallsSwitch }: { showTestCallsSwitch?: boolean }) => (
        <div data-testid="call-dashboard" data-test-calls-switch={String(Boolean(showTestCallsSwitch))} />
    ),
}));
vi.mock('@/components/wallet/WalletBalanceCard', () => ({
    WalletBalanceCard: () => <div data-testid="wallet-card" />,
}));

import OverviewPage from './page';

// jsdom's own hostname is "localhost": pointing the client URL at it makes this the client domain.
const ON_CLIENT_DOMAIN = 'http://localhost:3000';

describe('OverviewPage', () => {
    beforeEach(() => {
        getAuthUser.mockReset();
        getAuthUser.mockResolvedValue({ data: { is_superuser: false } });
    });

    afterEach(() => {
        vi.unstubAllEnvs();
    });

    describe('on the client domain', () => {
        beforeEach(() => vi.stubEnv('NEXT_PUBLIC_CLIENT_URL', ON_CLIENT_DOMAIN));

        it('shows the dashboard and the wallet, and no admin tools', async () => {
            render(<OverviewPage />);

            expect(await screen.findByTestId('call-dashboard')).toBeTruthy();
            expect(screen.getByTestId('wallet-card')).toBeTruthy();
            expect(screen.getByText('Welcome, Priya!')).toBeTruthy();
            expect(screen.getByText('Here is how your voice agents are performing.')).toBeTruthy();
            // Test calls are an admin concern: a client's dashboard never offers the switch.
            expect(screen.getByTestId('call-dashboard').getAttribute('data-test-calls-switch')).toBe('false');
            expect(screen.queryByText('Quick actions')).toBeNull();
            expect(screen.queryByText('Configure models')).toBeNull();
            expect(screen.queryByText('Super Admin')).toBeNull();
        });

        it('never checks whether the viewer is a superuser', async () => {
            render(<OverviewPage />);
            await screen.findByTestId('call-dashboard');

            expect(getAuthUser).not.toHaveBeenCalled();
        });
    });

    describe('on the admin domain', () => {
        it('shows the same dashboard, with quick actions above it and no wallet', async () => {
            render(<OverviewPage />);

            expect(await screen.findByTestId('call-dashboard')).toBeTruthy();
            expect(screen.getByTestId('call-dashboard').getAttribute('data-test-calls-switch')).toBe('true');
            expect(screen.getByText('Quick actions')).toBeTruthy();
            expect(screen.getByRole('link', { name: 'Agents' }).getAttribute('href')).toBe('/workflow');
            expect(screen.getByRole('link', { name: 'Configure models' }).getAttribute('href')).toBe(
                '/model-configurations',
            );
            expect(screen.queryByTestId('wallet-card')).toBeNull();
        });

        it('says the data is for the organization the viewer has selected', async () => {
            render(<OverviewPage />);

            expect(
                await screen.findByText('Here is how the voice agents of the organization you have selected are performing.'),
            ).toBeTruthy();
        });

        it('offers Super Admin only to a superuser', async () => {
            const { unmount } = render(<OverviewPage />);
            await screen.findByText('Quick actions');
            await waitFor(() => expect(getAuthUser).toHaveBeenCalled());
            expect(screen.queryByRole('link', { name: 'Super Admin' })).toBeNull();
            unmount();

            getAuthUser.mockResolvedValue({ data: { is_superuser: true } });
            render(<OverviewPage />);

            const link = await screen.findByRole('link', { name: 'Super Admin' });
            expect(link.getAttribute('href')).toBe('/superadmin');
        });
    });
});
