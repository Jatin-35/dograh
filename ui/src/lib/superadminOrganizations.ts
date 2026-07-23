import { client } from "@/client/client.gen";

/**
 * Thin wrappers over the superuser organization endpoints.
 *
 * These call the shared generated `client` directly (rather than typed SDK
 * functions) because the endpoints were added after the last
 * `npm run generate-client`. Regenerating the client will produce typed
 * `...ApiV1SuperuserOrganizations...` helpers that can replace these; until
 * then the raw client still routes through the same base URL + auth
 * interceptor as every other request.
 */

export type OrganizationStatus = "pending_setup" | "active" | "suspended";

export interface SuperadminOrganization {
    id: number;
    provider_id: string;
    name: string | null;
    primary_contact_email: string | null;
    status: OrganizationStatus;
    created_at: string;
    user_count: number;
}

interface OrganizationsListResponse {
    organizations: SuperadminOrganization[];
    total_count: number;
}

interface CreateOrganizationResponse {
    organization: SuperadminOrganization;
    invitation_sent: boolean;
}

export async function listSuperadminOrganizations(): Promise<SuperadminOrganization[]> {
    const { data, error } = await client.get<OrganizationsListResponse>({
        url: "/api/v1/superuser/organizations",
    });
    if (error || !data) {
        throw new Error(
            typeof error === "string" ? error : "Failed to load organizations",
        );
    }
    return data.organizations;
}

export async function createSuperadminOrganization(
    name: string,
    email: string,
): Promise<CreateOrganizationResponse> {
    const { data, error } = await client.post<CreateOrganizationResponse>({
        url: "/api/v1/superuser/organizations",
        body: { name, email },
    });
    if (error || !data) {
        throw new Error(
            typeof error === "string" ? error : "Failed to create organization",
        );
    }
    return data;
}

export async function updateSuperadminOrganizationStatus(
    organizationId: number,
    status: OrganizationStatus,
): Promise<SuperadminOrganization> {
    const { data, error } = await client.patch<SuperadminOrganization>({
        url: `/api/v1/superuser/organizations/${organizationId}/status`,
        body: { status },
    });
    if (error || !data) {
        throw new Error(
            typeof error === "string" ? error : "Failed to update organization status",
        );
    }
    return data;
}
