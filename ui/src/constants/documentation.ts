/**
 * Every documentation link in the product.
 *
 * The base lives in `./brand`, which reads it from the environment — see that
 * file for why. Add links here rather than inlining a URL at the call site: a
 * hardcoded `https://docs.…` somewhere in a page is invisible to that switch,
 * which is exactly how eleven of these drifted out of this file before.
 */

import { docsUrl } from "./brand";

export const NODE_DOCUMENTATION_URLS: Record<string, string> = {
    startCall: docsUrl("voice-agent/start-call"),
    endCall: docsUrl("voice-agent/end-call"),
    agent: docsUrl("voice-agent/agent"),
    global: docsUrl("voice-agent/global"),
    apiTrigger: docsUrl("voice-agent/api-trigger"),
    webhook: docsUrl("voice-agent/webhook"),
    qaAnalysis: docsUrl("getting-started"),
};

export const CONTEXT_VARIABLES_DOC_URL = docsUrl("core-concepts/context-and-variables");

export const TOOLS_INTRODUCTION_DOC_URL = docsUrl("voice-agent/tools/introduction");

export const KNOWLEDGE_BASE_DOC_URL = docsUrl("voice-agent/knowledge-base");

export const PRE_CALL_DATA_FETCH_DOC_URL = docsUrl("voice-agent/pre-call-data-fetch");

export const SETTINGS_DOCUMENTATION_URLS: Record<string, string> = {
    general: docsUrl("voice-agent/editing-a-workflow"),
    modelOverrides: docsUrl("configurations/inference-providers"),
    templateVariables: docsUrl("voice-agent/template-variables"),

    recordings: docsUrl("voice-agent/pre-recorded-audio"),
    deployment: docsUrl("voice-agent/add-to-website"),
};

export const WIDGET_MODE_DOCUMENTATION_URLS: Record<"floating" | "inline" | "headless", string> = {
    floating: docsUrl("voice-agent/add-to-website#floating-widget"),
    inline: docsUrl("voice-agent/add-to-website#inline-component"),
    headless: docsUrl("voice-agent/add-to-website#headless-mode"),
};

export const TOOL_DOCUMENTATION_URLS: Record<string, string> = {
    http_api: docsUrl("voice-agent/tools/http-api"),
    end_call: docsUrl("voice-agent/tools/end-call"),
    transfer_call: docsUrl("voice-agent/tools/call-transfer"),
};

// ---------------------------------------------------------------------------
// Links that were previously written inline at their call sites. Same base,
// same switch — they were only scattered.
// ---------------------------------------------------------------------------

export const VOICE_AGENT_INTRODUCTION_DOC_URL = docsUrl("voice-agent/introduction");

export const API_KEYS_DOC_URL = docsUrl("configurations/api-keys");

export const SERVICE_KEYS_DOC_URL = docsUrl("configurations/api-keys#service-keys");

export const MCP_DOC_URL = docsUrl("integrations/mcp");

export const TRACING_DOC_URL = docsUrl("configurations/tracing");

export const TELEPHONY_OVERVIEW_DOC_URL = docsUrl("integrations/telephony/overview");

export const TELEPHONY_INBOUND_DOC_URL = docsUrl("integrations/telephony/inbound");

export const PRE_RECORDED_AUDIO_DOC_URL = docsUrl("voice-agent/pre-recorded-audio");

export const INTERRUPTION_DOC_URL = docsUrl("configurations/interruption");

export const DEPLOYMENT_UPDATE_DOC_URL = docsUrl("deployment/update");
