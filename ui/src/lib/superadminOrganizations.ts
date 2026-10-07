import { client } from "@/client/client.gen";
import { detailFromError } from "@/lib/apiError";

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
    /** Whether Scout, the in-editor AI assistant, is switched on for this org. */
    scout_enabled: boolean;
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

export async function updateSuperadminOrganizationScout(
    organizationId: number,
    enabled: boolean,
): Promise<SuperadminOrganization> {
    const { data, error } = await client.patch<SuperadminOrganization>({
        url: `/api/v1/superuser/organizations/${organizationId}/scout`,
        body: { enabled },
    });
    if (error || !data) {
        throw new Error(
            typeof error === "string" ? error : "Failed to update Scout access",
        );
    }
    return data;
}

export interface OrganizationInvitation {
    id: string;
    email: string | null;
    expires_at: string | null;
}

interface InvitationsResponse {
    invitations: OrganizationInvitation[];
    /** Where links sent now open. */
    link_opens_at: string;
}

/** Pending invitations into an organization. */
export async function listOrganizationInvitations(organizationId: number): Promise<InvitationsResponse> {
    const { data, error } = await client.get<InvitationsResponse>({
        url: `/api/v1/superuser/organizations/${organizationId}/invitations`,
    });
    if (error || !data) {
        throw new Error(detailFromError(error, "Failed to load invitations"));
    }
    return data;
}

interface SentInvitation {
    email: string;
    /** Where the emailed link opens. */
    link_opens_at: string;
}

/** Email a fresh invitation (replaces a pending one to the same address). */
export async function sendOrganizationInvitation(
    organizationId: number,
    email: string,
): Promise<SentInvitation> {
    const { data, error } = await client.post<SentInvitation>({
        url: `/api/v1/superuser/organizations/${organizationId}/invitations`,
        body: { email },
    });
    if (error || !data) {
        throw new Error(detailFromError(error, "Failed to send the invitation"));
    }
    return data;
}

/** Withdraw a pending invitation so its link stops working. */
export async function revokeOrganizationInvitation(organizationId: number, invitationId: string): Promise<void> {
    const { error } = await client.delete({
        url: `/api/v1/superuser/organizations/${organizationId}/invitations/${invitationId}`,
    });
    if (error) {
        throw new Error(detailFromError(error, "Failed to revoke the invitation"));
    }
}
