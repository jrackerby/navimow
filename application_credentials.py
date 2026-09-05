"""Register Navimow's fixed OAuth2 client.

WHY THIS PLATFORM AND NOT NavimowHA's async_setup HOOK. NavimowHA registered
its implementation from the component's own `async_setup`, which only runs
once the component is imported -- so the config flow could not rely on it
being there and re-registered the implementation on every read of its
`oauth2_implementation` property, as a side effect of a property getter.
application_credentials is the supported hook and runs before the flow does.

`async_get_auth_implementation`, not `async_get_authorization_server`: the
latter makes core build a stock AuthImplementation, which would discard both
of auth.py's reasons for existing -- the channel=homeassistant parameter the
vendor login page keys on, and telling a revoked grant apart from an
unreachable token endpoint.
"""

from __future__ import annotations

from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .auth import NavimowOAuth2Implementation
from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Kept for completeness; core prefers the implementation hook below."""
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    return NavimowOAuth2Implementation(
        hass, auth_domain, credential.client_id, credential.client_secret
    )
