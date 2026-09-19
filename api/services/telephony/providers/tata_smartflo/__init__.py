"""TATA SmartFlo telephony provider package.

Registry name is ``tata_smartflo`` rather than anything hyphenated: the name is
interpolated into a Python module path by
``api/routes/telephony.py::_mount_provider_routers`` and is also a
``WorkflowRunMode`` value. A hyphen there produces an invalid module path whose
``ModuleNotFoundError`` is caught and read as "this provider has no routes" —
so routes would silently never mount. The customer-facing name lives in
``ui_metadata.display_name``, which is free-form.
"""

from typing import Any, Dict

from api.services.telephony.registry import (
    ProviderSpec,
    ProviderUIField,
    ProviderUIMetadata,
    register,
)

from .config import (
    TataSmartfloConfigurationRequest,
    TataSmartfloConfigurationResponse,
)
from .provider import TataSmartfloProvider
from .transport import create_transport


def _config_loader(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider": "tata_smartflo",
        "api_base": value.get("api_base"),
        "email": value.get("email"),
        "password": value.get("password"),
        "api_key": value.get("api_key"),
        "caller_id": value.get("caller_id"),
        "from_numbers": value.get("from_numbers", []),
        "connect_secret": value.get("connect_secret"),
    }


_UI_METADATA = ProviderUIMetadata(
    display_name="TATA SmartFlo",
    docs_url="https://docs.botrixai.com/integrations/telephony/tata-smartflo",
    fields=[
        ProviderUIField(
            name="api_base",
            label="API Base URL",
            type="text",
            required=False,
            description="SmartFlo API base URL",
            placeholder="https://api-smartflo.tatateleservices.com",
        ),
        ProviderUIField(
            name="email",
            label="Login Email",
            type="text",
            sensitive=True,
            description=(
                "SmartFlo login id. SmartFlo issues no long-lived API token — "
                "credentials are exchanged for a bearer token that expires "
                "roughly hourly, and refreshed automatically."
            ),
        ),
        ProviderUIField(
            name="password",
            label="Password",
            type="password",
            sensitive=True,
            description=(
                "SmartFlo account password. Note SmartFlo enforces a 90-day "
                "password expiry; calling stops when it lapses."
            ),
        ),
        ProviderUIField(
            name="api_key",
            label="Click-to-Call API Key",
            type="password",
            required=False,
            sensitive=True,
            description=(
                "Required for outbound calls only. Generated under API Connect "
                "→ Click to Call Support API, and it selects which voice bot "
                "SmartFlo streams the call to. Inbound never uses it."
            ),
        ),
        ProviderUIField(
            name="caller_id",
            label="Caller ID",
            type="text",
            required=False,
            description=(
                "Default caller id for outbound calls. Must be a DID registered "
                "to this SmartFlo account."
            ),
        ),
        ProviderUIField(
            name="from_numbers",
            label="Phone Numbers",
            type="string-array",
            description=(
                "DIDs on this account. Inbound calls are routed by matching the "
                "dialled number against these."
            ),
        ),
        ProviderUIField(
            name="connect_secret",
            label="Connect Secret",
            type="password",
            required=False,
            sensitive=True,
            description=(
                "Shared secret required on inbound voice-streaming requests. "
                "SmartFlo documents no request signing, so without this the "
                "connect endpoint is open to anyone who learns the URL."
            ),
        ),
    ],
)


SPEC = ProviderSpec(
    name="tata_smartflo",
    provider_cls=TataSmartfloProvider,
    config_loader=_config_loader,
    transport_factory=create_transport,
    # SmartFlo streams 8 kHz mu-law; see SMARTFLO_WIRE_SAMPLE_RATE.
    transport_sample_rate=8000,
    config_request_cls=TataSmartfloConfigurationRequest,
    config_response_cls=TataSmartfloConfigurationResponse,
    ui_metadata=_UI_METADATA,
    # Inbound webhooks are matched to a stored config by login id; SmartFlo has
    # no separate account-id concept exposed on the webhook payload.
    account_id_credential_field="email",
)


register(SPEC)


__all__ = [
    "SPEC",
    "TataSmartfloConfigurationRequest",
    "TataSmartfloConfigurationResponse",
    "TataSmartfloProvider",
    "create_transport",
]
