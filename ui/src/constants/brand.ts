/**
 * Every outbound link that carries a brand, in one place.
 *
 * This is a self-hosted fork that still merges from upstream, so these are read
 * from the environment rather than edited in place. A find-and-replace across
 * the twenty-odd files that link out would conflict on *every* upstream change
 * to any of them; reading a variable leaves the code byte-identical to upstream
 * and puts the difference in `.env`, where there is nothing to re-resolve.
 *
 * These are `NEXT_PUBLIC_*`, which Next.js inlines at **build** time, not run
 * time. Changing one in `.env` does nothing until the `ui` image is rebuilt —
 * see the build args in docker-compose.override.yaml. A UI build that reports
 * CACHED after changing these has not picked them up.
 *
 * Each has a default so a plain `git clone && npm run dev` still works with no
 * configuration.
 */

/** Base URL of the documentation site every "Learn more" link points at. */
export const DOCS_BASE_URL = (
    process.env.NEXT_PUBLIC_DOCS_URL || "https://docs.botrixai.com"
).replace(/\/+$/, "");

/** Marketing/company site, for legal and contact links. */
export const COMPANY_BASE_URL = (
    process.env.NEXT_PUBLIC_COMPANY_URL || "https://www.botrixai.com"
).replace(/\/+$/, "");

/** Join a path onto a base without doubling or dropping the slash. */
function join(base: string, path: string): string {
    return `${base}/${path.replace(/^\/+/, "")}`;
}

/** A documentation page. Pass the path only — never a full URL, or the
 * variable stops being the single point of control this file exists to be. */
export function docsUrl(path: string): string {
    return join(DOCS_BASE_URL, path);
}

/** Legal and contact pages, on the company site rather than the docs site.
 *
 * These matter more than the documentation links: a customer clicking "Terms
 * of Service" inside this product must not land on another company's terms —
 * those are not the terms they agreed to.
 */
export const PRIVACY_POLICY_URL = join(COMPANY_BASE_URL, "privacy-policy");
export const TERMS_OF_SERVICE_URL = join(COMPANY_BASE_URL, "terms-of-service");
export const CONTACT_URL = join(COMPANY_BASE_URL, "contact");
