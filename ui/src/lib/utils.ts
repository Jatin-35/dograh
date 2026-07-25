import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

import { getAuthUserApiV1UserAuthUserGet } from "@/client/sdk.gen";
import { getWorkflowCountApiV1WorkflowCountGet } from "@/client/sdk.gen";
import { impersonateApiV1SuperuserImpersonatePost } from "@/client/sdk.gen";
import { detailFromError } from "@/lib/apiError";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function getRandomId() {
  return Math.floor(Math.random() * 10_000);
}

export function getNextNodeId(existingNodes: { id: string }[]): string {
  const numericIds = existingNodes
    .map(node => parseInt(node.id, 10))
    .filter(id => !isNaN(id));

  const maxId = numericIds.length > 0 ? Math.max(...numericIds) : 0;
  return String(maxId + 1);
}

export function debounce<T extends (...args: unknown[]) => unknown>(func: T, wait: number): (...args: Parameters<T>) => void {
  let timeout: NodeJS.Timeout | null = null;

  return function (...args: Parameters<T>) {
    if (timeout) {
      clearTimeout(timeout);
    }

    timeout = setTimeout(() => {
      func(...args);
    }, wait);
  };
}

// Optional domain-aware routing for a genuine (non-impersonated) login:
// superusers always land on the admin domain, everyone else lands on the
// client-dashboard domain — regardless of which domain the login happened
// on. NEXT_PUBLIC_APP_URL is deliberately NOT used here: that domain is
// reserved for impersonation (see impersonateAsSuperadmin below), which is a
// separate code path that never goes through getRedirectUrl. Falls back to a
// same-origin relative path when unset (local dev, or single-domain
// deployments).
const ADMIN_BASE_URL = process.env.NEXT_PUBLIC_ADMIN_URL;
const CLIENT_BASE_URL = process.env.NEXT_PUBLIC_CLIENT_URL;

// Shared check for Stack's per-team 'admin' permission — the single source
// of truth for "does this org member have full access", used both for the
// post-login redirect below and for nav gating in AppSidebar.
export function hasAdminPermission(permissions: { id: string }[]): boolean {
  return permissions.some(p => p.id === 'admin');
}

function toDestination(path: string, baseUrl?: string): string {
  return baseUrl ? new URL(path, baseUrl).toString() : path;
}

export async function getRedirectUrl(token: string, permissions: { id: string }[] = []) {
  console.log('[getRedirectUrl] Called with:', {
    hasToken: !!token,
    tokenLength: token?.length,
    permissionsCount: permissions.length,
    permissions: permissions.map(p => p.id)
  });
  try {
    console.log('[getRedirectUrl] Calling getAuthUserApiV1UserAuthUserGet...');
    const authUser = await getAuthUserApiV1UserAuthUserGet({
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });
    console.log('[getRedirectUrl] Auth user response:', {
      hasData: !!authUser.data,
      isSuperuser: authUser.data?.is_superuser,
      userId: authUser.data?.id
    });
    if (authUser.data?.is_superuser) {
      console.log('[getRedirectUrl] User is superuser, redirecting to /superadmin');
      return toDestination("/superadmin", ADMIN_BASE_URL);
    }

    const isAdmin = hasAdminPermission(permissions);
    console.log('[getRedirectUrl] Admin permission check:', { hasAdminPermission: isAdmin });

  // If the user doesn't have admin permissions, redirect them to
  // usage page
  if (!isAdmin) {
    console.log('[getRedirectUrl] No admin permission, redirecting to /usage');
    return toDestination("/usage", CLIENT_BASE_URL);
  }

  // Check if user has any workflows
  try {
    console.log('[getRedirectUrl] Checking for existing workflows...');
    const countResponse = await getWorkflowCountApiV1WorkflowCountGet({
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    console.log('[getRedirectUrl] Found workflows:', {
      total: countResponse.data?.total,
      active: countResponse.data?.active
    });

    if (countResponse.data && countResponse.data.active > 0) {
      console.log('[getRedirectUrl] User has workflows, redirecting to /workflow');
      return toDestination("/workflow", CLIENT_BASE_URL);
    } else {
      console.log('[getRedirectUrl] No workflows found, redirecting to /workflow/create');
      return toDestination("/workflow/create", CLIENT_BASE_URL);
    }
  } catch (error) {
    console.error('[getRedirectUrl] Error checking workflows:', error);
    // If we can't check workflows, default to /workflow/create
    console.log('[getRedirectUrl] Defaulting to /workflow/create due to error');
    return toDestination("/workflow/create", CLIENT_BASE_URL);
  }
  } catch (error) {
    console.error("[getRedirectUrl] Failed to fetch auth user:", error);
    // Re-throw the error so the caller can handle it
    throw error;
  }
}


/**
 * Centralised impersonation logic to avoid code duplication between pages.
 *
 * It performs the super-admin impersonate request, sends the Stack tokens to
 * the target-domain helper route, and optionally redirects the browser to the
 * supplied path.
 */
export async function impersonateAsSuperadmin(params: {
  accessToken: string;
  userId?: number;
  providerUserId?: string;
  email?: string;
  redirectPath?: string;
  /**
   * Forces the target account's Stack-selected team to this organization
   * before impersonating, instead of inheriting whatever team that account's
   * own session already had selected. Required whenever redirectPath deep-links
   * into a *specific* organization's data (e.g. a workflow editor) rather than
   * a generic destination like "/workflow" that works under any team.
   */
  targetOrganizationId?: number;
  /**
   * If true the browser opens the impersonated session in a **new tab**
   * (via `window.open`). Defaults to `false` which navigates in the current tab.
   */
  openInNewTab?: boolean;
}): Promise<void> {
  const {
    accessToken: adminAccessToken,
    userId,
    providerUserId,
    email,
    redirectPath,
    targetOrganizationId,
    openInNewTab = false,
  } = params;
  const targetWindow = openInNewTab ? window.open('', '_blank') : null;
  if (targetWindow) {
    targetWindow.opener = null;
  }
  if (openInNewTab && !targetWindow) {
    throw new Error('Unable to open impersonation tab. Please allow pop-ups and try again.');
  }

  // Build request body depending on which identifier we have.
  const hasIdentity = userId !== undefined || providerUserId !== undefined || email !== undefined;

  const body: Record<string, unknown> = {};
  if (userId !== undefined) {
    body.user_id = userId;
  }
  if (providerUserId !== undefined) {
    body.provider_user_id = providerUserId;
  }
  if (email !== undefined) {
    body.email = email;
  }
  if (targetOrganizationId !== undefined) {
    body.target_organization_id = targetOrganizationId;
  }

  if (!hasIdentity) {
    targetWindow?.close();
    throw new Error('Either userId, providerUserId, or email must be provided');
  }

  let resp: Awaited<ReturnType<typeof impersonateApiV1SuperuserImpersonatePost>>;
  try {
    resp = await impersonateApiV1SuperuserImpersonatePost({
      body,
      headers: {
        Authorization: `Bearer ${adminAccessToken}`,
      },
    });
  } catch (error) {
    targetWindow?.close();
    throw error;
  }

  if (resp.error) {
    targetWindow?.close();
    throw new Error(detailFromError(resp.error, 'Failed to impersonate user'));
  }

  const refreshToken = resp.data?.refresh_token;
  if (!refreshToken) {
    targetWindow?.close();
    throw new Error('No refresh token returned from impersonate');
  }

  // ---------------------------------------------------------------------------------
  // Instead of setting the cookie here (which would also affect the superadmin
  // sub-domain), redirect the browser to the dedicated impersonation helper route
  // (served from the target sub-domain, e.g. app.dograh.com). The route will set the
  // cookie for the *current* sub-domain only and then forward the user to the final
  // destination.
  // ---------------------------------------------------------------------------------

  // Determine the base URL that should handle the impersonation cookie. Configured
  // via NEXT_PUBLIC_APP_URL (e.g. https://app.dograh.com); falls back to the current
  // origin (e.g. localhost, staging, or already on the app).
  const appBaseUrl = process.env.NEXT_PUBLIC_APP_URL || window.location.origin;

  const finalRedirect = redirectPath ?? '/workflow';

  // Build the redirect URL to the helper route, passing along the refresh token and
  // the final destination.
  const impersonateUrl = new URL('/impersonate', appBaseUrl);
  impersonateUrl.searchParams.set('refresh_token', refreshToken);
  impersonateUrl.searchParams.set('redirect_path', finalRedirect);

  if (openInNewTab) {
    if (!targetWindow) {
      throw new Error('Unable to open impersonation tab. Please allow pop-ups and try again.');
    }
    targetWindow.location.href = impersonateUrl.toString();
  } else {
    window.location.href = impersonateUrl.toString();
  }
}
