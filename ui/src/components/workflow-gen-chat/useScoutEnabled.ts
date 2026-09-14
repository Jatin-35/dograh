"use client";

import { useAppConfig } from "@/context/AppConfigContext";
import { useOrgConfig } from "@/context/OrgConfigContext";

interface ScoutAvailability {
    /** True only when both gates are open and both have actually been read. */
    enabled: boolean;
    /** Still resolving one of the two sources — render nothing rather than a flash. */
    loading: boolean;
    /** The deployment has the assistant's credentials at all. */
    configuredOnDeployment: boolean;
    /** A superadmin switched Scout on for the signed-in organization. */
    enabledForOrganization: boolean;
}

/**
 * Whether Scout should be offered to the signed-in user.
 *
 * Two independent gates, both required, matching `_require_scout_enabled` on
 * the backend: the deployment must be configured with assistant credentials,
 * and a superadmin must have enabled Scout for this organization. The org gate
 * defaults to off, so a client that has never been considered for Scout never
 * sees it. The UI check is presentation only — the server enforces the same
 * pair on every workflow-gen route.
 */
export function useScoutEnabled(): ScoutAvailability {
    const { config, loading: configLoading } = useAppConfig();
    const { orgContext, loading: orgLoading } = useOrgConfig();

    const configuredOnDeployment = Boolean(config?.workflowGenEnabled);
    const enabledForOrganization = Boolean(orgContext?.scout_enabled);
    const loading = configLoading || orgLoading;

    return {
        enabled: !loading && configuredOnDeployment && enabledForOrganization,
        loading,
        configuredOnDeployment,
        enabledForOrganization,
    };
}
