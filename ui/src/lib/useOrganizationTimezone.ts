'use client';

import { useEffect, useState } from 'react';

import { getPreferencesApiV1OrganizationsPreferencesGet } from '@/client/sdk.gen';
import { useAuth } from '@/lib/auth';

export function browserTimezone(): string {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

/**
 * The organization's timezone (its preference), which decides what "today" is on
 * the dashboard and what clock time a call happened at on a run. `null` until it
 * is known; the browser's timezone if the organization has none or the lookup fails.
 *
 * Deliberately not cached across calls: the preference can change while the app
 * is open (or the viewer can switch organization), and a stale timezone would
 * quietly show the wrong day.
 */
export function useOrganizationTimezone(): string | null {
    const auth = useAuth();
    const [timezone, setTimezone] = useState<string | null>(null);

    useEffect(() => {
        if (!auth.isAuthenticated || auth.loading) return;
        let cancelled = false;
        (async () => {
            try {
                const response = await getPreferencesApiV1OrganizationsPreferencesGet();
                if (!cancelled) setTimezone(response.data?.timezone || browserTimezone());
            } catch {
                if (!cancelled) setTimezone(browserTimezone());
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [auth.isAuthenticated, auth.loading]);

    return timezone;
}
