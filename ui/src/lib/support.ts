// Configuration for the in-app support affordance in the bottom-right corner.
//
// Two implementations share this module and are mutually exclusive:
//
//   * `ChatwootWidget` — a real live-chat inbox, shown only when this
//     deployment has been given a Chatwoot server *of its own*.
//   * `SupportGreeter` — our own branded card, shown otherwise.
//
// The default is deliberately the greeter. Upstream's image hardcoded
// dograh-hq's Chatwoot server and website token, which meant every client on
// this deployment saw Dograh's branding and — more importantly — opened
// support conversations in dograh-hq's helpdesk rather than ours. A website
// token names an inbox, so that was a live path for our clients' messages to
// land with a third party. The Dockerfile now leaves both values empty.
//
// Set both to switch live chat back on against an inbox we control; the
// greeter steps aside on its own, with no code change.

// Each of these must be spelled out in full: Next.js inlines
// `process.env.NEXT_PUBLIC_*` by literal match at build time, so a computed
// lookup would silently read `undefined` in the browser bundle.
export const CHATWOOT_BASE_URL = process.env.NEXT_PUBLIC_CHATWOOT_URL ?? "";
export const CHATWOOT_WEBSITE_TOKEN = process.env.NEXT_PUBLIC_CHATWOOT_TOKEN ?? "";

/** A Chatwoot inbox has been configured for this deployment. */
export const isChatwootConfigured = Boolean(
    CHATWOOT_BASE_URL && CHATWOOT_WEBSITE_TOKEN,
);

// Contact details for the greeter. All optional: whatever is set is rendered,
// and with none of them set the card is a plain greeting. Nothing here is
// guessed — an unset value shows nothing rather than a placeholder address
// that would bounce.
export const SUPPORT_EMAIL = (process.env.NEXT_PUBLIC_SUPPORT_EMAIL ?? "").trim();
export const SUPPORT_PHONE = (process.env.NEXT_PUBLIC_SUPPORT_PHONE ?? "").trim();
export const SUPPORT_WHATSAPP = (
    process.env.NEXT_PUBLIC_SUPPORT_WHATSAPP ?? ""
).trim();

/** wa.me requires the number in international format with no punctuation. */
export const whatsappHref = (number: string) =>
    `https://wa.me/${number.replace(/[^\d]/g, "")}`;

// Surfaces that own the bottom-right corner themselves, where a floating
// bubble would sit on top of something the user needs: the workflow builder
// (the in-app call tester) and the code editor (the test-run panel and Scout).
// `/workflow/create` and the `/workflow` list are not builders and keep it.
const CORNER_OCCUPIED = [
    /^\/workflow\/(?!create(?:$|\/))[^/]+(?:\/.*)?$/,
    /^\/code-editor(?:$|\/)/,
];

/** This route needs its bottom-right corner left alone. */
export const hidesSupportLauncher = (pathname: string) =>
    CORNER_OCCUPIED.some((pattern) => pattern.test(pathname));
