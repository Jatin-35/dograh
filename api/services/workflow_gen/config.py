"""Feature flag and settings for the in-product AI assistant.

Deployment-wide, BYO credentials — separate from any per-org LLM
configuration used elsewhere in the app. `is_workflow_gen_configured()` is
the single source of truth for whether the feature is enabled; both the
`/health` endpoint and the route layer call it.
"""

from api.constants import (
    WF_GEN_AZURE_OPENAI_API_KEY,
    WF_GEN_AZURE_OPENAI_API_VERSION,
    WF_GEN_AZURE_OPENAI_DEPLOYMENT,
    WF_GEN_AZURE_OPENAI_ENDPOINT,
    WF_GEN_LLM_PROVIDER,
)

SUPPORTED_PROVIDERS = {"azure_openai"}


def is_workflow_gen_configured() -> bool:
    if WF_GEN_LLM_PROVIDER not in SUPPORTED_PROVIDERS:
        return False
    return bool(
        WF_GEN_AZURE_OPENAI_API_KEY
        and WF_GEN_AZURE_OPENAI_ENDPOINT
        and WF_GEN_AZURE_OPENAI_DEPLOYMENT
        and WF_GEN_AZURE_OPENAI_API_VERSION
    )
