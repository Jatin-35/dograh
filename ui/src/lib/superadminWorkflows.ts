import { client } from "@/client/client.gen";

/**
 * Thin wrapper over the superuser workflows endpoint. Calls the shared
 * generated `client` directly (rather than a typed SDK function) since this
 * endpoint postdates the last `npm run generate-client` — same approach as
 * superadminOrganizations.ts.
 */

export interface SuperadminWorkflow {
    id: number;
    name: string;
    created_at: string;
    total_runs: number;
    folder_id: number | null;
    folder_name: string | null;
    organization_id: number;
    organization_name: string | null;
    organization_provider_id: string;
    organization_status: "pending_setup" | "active" | "suspended";
    organization_primary_contact_email: string | null;
}

interface WorkflowsListResponse {
    workflows: SuperadminWorkflow[];
    total_count: number;
}

export async function listSuperadminWorkflows(): Promise<SuperadminWorkflow[]> {
    const { data, error } = await client.get<WorkflowsListResponse>({
        url: "/api/v1/superuser/workflows",
    });
    if (error || !data) {
        throw new Error(
            typeof error === "string" ? error : "Failed to load agents",
        );
    }
    return data.workflows;
}
