"""Sarvam LLM on the pipecat we ship, kept in step with Sarvam's current models.

The pinned pipecat (dograh-hq/pipecat at the v1.5 merge) only accepts
``sarvam-30b``/``-16k`` and ``sarvam-105b``/``-32k`` and refuses anything else
at pipeline start. Sarvam has since withdrawn ``sarvam-30b`` (upstream dograh
d747b038) and added ``sarvam-105b-conversations`` (upstream dograh 06f8cf79,
pipecat v1.12). Rather than upgrade pipecat for the whole app, this service:

- also accepts ``sarvam-105b-conversations``. Like pipecat v1.12 it goes to the
  ``/v1`` endpoint, and the reasoning and wiki-grounding options, which that
  model does not take, are never sent with it;
- moves a saved withdrawn model to ``sarvam-105b`` when the call starts (see
  ``resolve_sarvam_model``), so agents saved before the change keep working.

Our pipecat already sends every model to ``/v1``; the ``/v2`` endpoint that
upstream's v1.12 defaults to is invite-only and answers 400 (dograh #838).
"""

from __future__ import annotations

from loguru import logger

from pipecat.services.sarvam.llm import SarvamLLMService

SARVAM_DEFAULT_BASE_URL = "https://api.sarvam.ai/v1"
SARVAM_CONVERSATIONS_MODEL = "sarvam-105b-conversations"

# Models Sarvam withdrew, and what an agent still saved with one now uses.
WITHDRAWN_SARVAM_MODELS = {
    "sarvam-30b": "sarvam-105b",
    "sarvam-30b-16k": "sarvam-105b",
}


def resolve_sarvam_model(model: str | None) -> str:
    """The model to call: a withdrawn one becomes its replacement (logged)."""
    if not model:
        return "sarvam-105b"
    replacement = WITHDRAWN_SARVAM_MODELS.get(model)
    if replacement:
        logger.warning(
            f"Sarvam model '{model}' has been withdrawn by Sarvam; using "
            f"'{replacement}' instead. Update the agent's LLM settings to stop "
            "this warning."
        )
        return replacement
    return model


class DograhSarvamLLMService(SarvamLLMService):
    """``SarvamLLMService`` that also accepts ``sarvam-105b-conversations``."""

    _SUPPORTED_MODELS = SarvamLLMService._SUPPORTED_MODELS | {
        SARVAM_CONVERSATIONS_MODEL
    }
    # Models that reject Sarvam's reasoning_effort and wiki_grounding options.
    _PLAIN_CHAT_MODELS = frozenset({SARVAM_CONVERSATIONS_MODEL})

    def build_chat_completion_params(self, params_from_context) -> dict:
        params = super().build_chat_completion_params(params_from_context)
        if params.get("model") in self._PLAIN_CHAT_MODELS:
            params.pop("reasoning_effort", None)
            params.pop("wiki_grounding", None)
        return params
