/** Keeping clients on the client-dashboard domain.
 *
 * Domain-separated deployments have three: NEXT_PUBLIC_ADMIN_URL (superusers),
 * NEXT_PUBLIC_APP_URL (a superuser impersonating a client to build their
 * agents; everyone there gets the full menu) and NEXT_PUBLIC_CLIENT_URL
 * (clients, restricted menu). Sign-in already sends a client to the client
 * domain, but nothing kept them there: a signed-in client opening an admin or
 * app address stayed on it. This is the decision; ClientDomainGuard acts on it.
 *
 * It is navigation, not access control: the server is what refuses superuser
 * routes, and client restrictions on the server are separate work.
 */

/** Set by /impersonate on the app domain: the Stack user id being impersonated. */
export const IMPERSONATION_COOKIE = "botrix-impersonating";

/** Auth and helper routes that must run where they are. */
const PASS_THROUGH = ["/handler", "/after-sign-in", "/impersonate", "/api"];

function hostOf(url: string | undefined): string | null {
    if (!url) return null;
    try {
        return new URL(url).hostname;
    } catch {
        return null;
    }
}

export interface ClientDomainInput {
    hostname: string;
    pathname: string;
    search: string;
    /** The signed-in Stack user's id. */
    userId: string;
    isSuperuser: boolean;
    /** IMPERSONATION_COOKIE's value, if present. */
    impersonatedUserId: string | null;
    adminUrl?: string;
    appUrl?: string;
    clientUrl?: string;
}

/** Superadmin screens: never shown on the client domain. */
const ADMIN_ONLY = ["/superadmin", "/impersonate"];

const startsWithAny = (pathname: string, prefixes: string[]) =>
    prefixes.some((p) => pathname === p || pathname.startsWith(`${p}/`));

/** Whether this host is the client dashboard domain (nothing admin renders there). */
export function isClientDomain(hostname: string, clientUrl: string | undefined): boolean {
    const clientHost = hostOf(clientUrl);
    return Boolean(clientHost) && hostname === clientHost;
}

/** Where to send this signed-in user, or null to stay. */
export function clientRedirectTarget(input: ClientDomainInput): string | null {
    const clientHost = hostOf(input.clientUrl);
    if (!input.clientUrl || !clientHost) return null; // single-domain install
    const here = `${input.pathname}${input.search}`;

    if (input.hostname === clientHost) {
        // The client domain only ever shows client screens: a superuser works
        // on the admin domain, and admin screens never render here.
        if (startsWithAny(input.pathname, PASS_THROUGH.filter((p) => p !== "/impersonate"))) {
            return null;
        }
        const adminUrl = hostOf(input.adminUrl) ? input.adminUrl : undefined;
        if (input.isSuperuser && adminUrl) return new URL(here, adminUrl).toString();
        if (startsWithAny(input.pathname, ADMIN_ONLY)) {
            return new URL("/overview", input.clientUrl).toString();
        }
        return null;
    }

    if (input.isSuperuser) return null;
    if (startsWithAny(input.pathname, PASS_THROUGH)) return null;

    const onAdmin = input.hostname === hostOf(input.adminUrl);
    const onApp = input.hostname === hostOf(input.appUrl);
    if (!onAdmin && !onApp) return null;
    // A superuser impersonating this very account builds its agents here.
    if (onApp && input.impersonatedUserId && input.impersonatedUserId === input.userId) {
        return null;
    }
    return new URL(here, input.clientUrl).toString();
}

/** The Stack user id in an access token (its unverified `sub` claim). */
export function subjectOfAccessToken(token: string): string | null {
    try {
        const payload = token.split(".")[1];
        if (!payload) return null;
        const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
        const sub = (JSON.parse(json) as { sub?: unknown }).sub;
        return typeof sub === "string" && sub ? sub : null;
    } catch {
        return null;
    }
}

export function readCookie(name: string, cookieString: string): string | null {
    for (const part of cookieString.split(";")) {
        const [key, ...rest] = part.trim().split("=");
        if (key === name) return decodeURIComponent(rest.join("="));
    }
    return null;
}
