import { updatePhoneNumberApiV1OrganizationsTelephonyConfigsConfigIdPhoneNumbersPhoneNumberIdPut } from "@/client/sdk.gen";
import type { PhoneNumberResponse, PhoneNumberUpdateRequest } from "@/client/types.gen";
import { detailFromError } from "@/lib/apiError";

/**
 * A phone number's channels: how many calls may run on it at once.
 *
 * `max_concurrent_calls` was added after the last `npm run generate-client`,
 * so it is typed here; a regenerated client carries it on the generated types.
 */
export type PhoneNumberWithChannels = PhoneNumberResponse & {
    max_concurrent_calls?: number;
};

/** Channels shown for a number; rows from before the setting existed are 1. */
export function channelsOf(phoneNumber: PhoneNumberWithChannels): number {
    return phoneNumber.max_concurrent_calls ?? 1;
}

export const MIN_CHANNELS = 1;
export const MAX_CHANNELS = 200;

/** Update a number's channels through the organization-scoped update route. */
export async function updatePhoneNumberChannels(
    accessToken: string,
    configId: number,
    phoneNumberId: number,
    maxConcurrentCalls: number,
): Promise<PhoneNumberWithChannels> {
    const body: PhoneNumberUpdateRequest & { max_concurrent_calls: number } = {
        max_concurrent_calls: maxConcurrentCalls,
    };
    const response =
        await updatePhoneNumberApiV1OrganizationsTelephonyConfigsConfigIdPhoneNumbersPhoneNumberIdPut({
            headers: { Authorization: `Bearer ${accessToken}` },
            path: { config_id: configId, phone_number_id: phoneNumberId },
            body,
        });
    if (response.error || !response.data) {
        throw new Error(detailFromError(response.error, "Failed to update channels"));
    }
    return response.data as PhoneNumberWithChannels;
}
