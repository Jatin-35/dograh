import { client } from "@/client/client.gen";
import type { PhoneNumberResponse } from "@/client/types.gen";
import { detailFromError } from "@/lib/apiError";

/**
 * Superuser-only phone number settings.
 *
 * Calls the shared generated `client` directly, like the other superadmin
 * wrappers, because the endpoint was added after the last
 * `npm run generate-client`. A regenerated client makes these typed helpers
 * redundant.
 */

/** A phone number with its channel count (concurrent calls it allows). */
export type PhoneNumberWithChannels = PhoneNumberResponse & {
    max_concurrent_calls?: number;
};

/** Channels shown for a number; rows from before the setting existed are 1. */
export function channelsOf(phoneNumber: PhoneNumberWithChannels): number {
    return phoneNumber.max_concurrent_calls ?? 1;
}

export const MIN_CHANNELS = 1;
export const MAX_CHANNELS = 200;

export async function updatePhoneNumberChannels(
    phoneNumberId: number,
    maxConcurrentCalls: number,
): Promise<PhoneNumberWithChannels> {
    const { data, error } = await client.patch<{ 200: PhoneNumberWithChannels }>({
        url: `/api/v1/superuser/phone-numbers/${phoneNumberId}/channels`,
        body: { max_concurrent_calls: maxConcurrentCalls },
    });
    if (error || !data) {
        throw new Error(detailFromError(error, "Failed to update channels"));
    }
    return data;
}
