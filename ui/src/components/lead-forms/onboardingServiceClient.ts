// Thin client for the SEPARATE user_onboarding service (its own base URL).
// Not part of the generated BotrixAI SDK — a different host. All endpoints are PUBLIC
// (no auth token); identity is the email carried in the body. Every call is
// BEST-EFFORT: failures are swallowed so a down/erroring service never blocks the user.

import { client } from "@/client/client.gen";

// Base URL of the user_onboarding service. Unset (the default — .env.example ships
// this commented out) → onboarding answers are not sent anywhere, and lead forms
// (hire-expert / enterprise) go to our own API, which emails them to the team.
const BASE_URL = process.env.NEXT_PUBLIC_ONBOARDING_API_URL || "";

// Bound every call so a slow/hung service can never freeze the UI. Best-effort:
// failures are surfaced via console.error (Sentry breadcrumbs) but never thrown.
const TIMEOUT_MS = 6000;

// Shape the lead endpoints return: ok + the server's calendar verdict. The decision is
// server-side — the app only RENDERS show_calendar; it holds no qualification logic.
// `delivered` comes from our own backend: whether the lead email actually went out.
export type LeadResult = {
  ok: boolean;
  show_calendar?: boolean;
  cal_link?: string | null;
  delivered?: boolean;
};

// POST a JSON body to the onboarding service (public — no auth header). Returns the parsed
// body on success, or null on a non-2xx / network error / timeout (best-effort, never throws).
async function post(path: string, body: unknown): Promise<LeadResult | null> {
  if (!BASE_URL) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    // fetch does not reject on 4xx/5xx — check explicitly so dropped leads are
    // at least observable.
    if (!res.ok) {
      console.error(`[onboarding] POST ${path} failed with HTTP ${res.status}`);
      return null;
    }
    return (await res.json()) as LeadResult;
  } catch (err) {
    // Network error, or the timeout aborted the request. Never block the user.
    console.error(`[onboarding] POST ${path} did not complete:`, err);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Map a lead kind to its endpoint path on the onboarding service.
const LEAD_PATH: Record<"hire_expert" | "enterprise", string> = {
  hire_expert: "/api/v1/leads/hire-expert",
  enterprise: "/api/v1/leads/enterprise",
};

// Persist a lead submission (hire-expert / enterprise). Email is in the body. Returns the
// server's verdict (show_calendar / cal_link) so the modal can embed the calendar.
// Without an external leads service the lead goes to our own API, which emails it to the
// team (LEADS_EMAIL). Uses the shared generated client for the base URL and auth token.
export async function postLeadToService(
  kind: "hire_expert" | "enterprise",
  body: Record<string, unknown>,
): Promise<LeadResult | null> {
  if (BASE_URL) return post(LEAD_PATH[kind], body);
  const fields: Record<string, string> = {};
  for (const [key, value] of Object.entries(body)) {
    if (value !== undefined && value !== null && String(value).trim()) fields[key] = String(value);
  }
  try {
    const { data, error } = await client.post<LeadResult>({
      url: `/api/v1/leads/${kind}`,
      body: { fields },
    });
    if (error || !data) {
      console.error("[leads] submitting the lead failed:", error);
      return null;
    }
    // The ad-hoc client types `data` loosely; this route returns { ok, delivered }.
    return data as unknown as LeadResult;
  } catch (err) {
    console.error("[leads] submitting the lead did not complete:", err);
    return null;
  }
}

// Persist an onboarding submission (or skip — body carries `skipped`).
export async function postOnboardingToService(body: Record<string, unknown>): Promise<void> {
  await post("/api/v1/onboarding", body);
}
