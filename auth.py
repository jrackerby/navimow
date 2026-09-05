"""OAuth2 against the Navimow cloud.

The vendor's tokens last one to two days and the initial grant does NOT carry
a refresh_token. That is the single fact this file exists to handle honestly:
when there is nothing to refresh with, the only correct answer is to ask the
user to log in again, and saying so immediately is better than a week of
retries that cannot succeed.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.config_entry_oauth2_flow import LocalOAuth2Implementation

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN

_LOGGER = logging.getLogger(__name__)

# Substrings that mean the SERVER rejected the grant, as opposed to the
# request never arriving. Compared against a lowercased error string because
# the vendor endpoint returns prose, not a typed error body.
_DETERMINISTIC = ("401", "403", "invalid", "expired", "unauthorized", "forbidden")


class NavimowOAuth2Implementation(LocalOAuth2Implementation):
    """Navimow's OAuth2, with the refresh failure modes told apart."""

    def __init__(self, hass: HomeAssistant, domain: str, client_id: str, secret: str) -> None:
        super().__init__(
            hass=hass,
            domain=domain,
            client_id=client_id,
            client_secret=secret,
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
        )

    @property
    def name(self) -> str:
        return "Navimow"

    async def async_generate_authorize_url(self, *args: Any, **kwargs: Any) -> str:
        """Carry channel=homeassistant, which the vendor login page keys on."""
        url = await super().async_generate_authorize_url(*args, **kwargs)
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("channel", "homeassistant")
        return urlunparse(parsed._replace(query=urlencode(query)))

    async def _async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        """Refresh, distinguishing a revoked grant from an unreachable server.

        The distinction is load-bearing: ConfigEntryAuthFailed puts a repair
        card in front of the user and stops all polling, which is right for a
        dead grant and wrong for a DNS timeout. Raising the transient case
        unchanged lets the coordinator fall back to the cached access token
        instead.
        """
        if "refresh_token" not in token:
            raise ConfigEntryAuthFailed(
                "The Navimow access token has expired and the account was issued "
                "no refresh token. Please sign in again."
            )
        try:
            return await super()._async_refresh_token(token)
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001
            if any(marker in str(err).lower() for marker in _DETERMINISTIC):
                raise ConfigEntryAuthFailed(
                    "Navimow rejected the refresh token. Please sign in again."
                ) from err
            _LOGGER.debug("Navimow token refresh failed, likely transient: %s", err)
            raise
