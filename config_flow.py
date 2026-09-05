"""Config flow: OAuth2, one entry, with reauthentication.

Quality scale: `config-flow`, `unique-config-entry`, `reauthentication-flow`.
NavimowHA had no reauth step at all, which mattered because its tokens expire
in one to two days with no refresh token -- the state this flow exists to
recover from is the NORMAL end of a Navimow session, not an edge case.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class NavimowOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the Navimow OAuth2 flow."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One Navimow account per installation.

        `unique-config-entry`: a second entry would open a second MQTT session
        against the same broker credentials, and the broker drops the older
        one -- so two entries do not give you two accounts, they give you one
        that flaps.
        """
        if self._async_current_entries() and not self._reauth_entry_id():
            return self.async_abort(reason="single_instance_allowed")
        return await super().async_step_user(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Prove the token works on the channel the integration will use.

        LAW.md §9, and the rule the quality scale calls `test-before-configure`:
        a setup check that exercises a different channel than the one that will
        be used certifies nothing, and it certifies nothing GREEN, which is
        worse than no check. A completed OAuth redirect only proves the login
        page worked. The integration then talks to /openapi/smarthome/authList
        with a bearer token, so that is what gets called here, with the token
        that was just issued, before an entry exists to be broken.

        The worked example in §9 is this exact shape: a config flow probed a
        host's telemetry daemon while the integration polled over ssh, an entry
        was created against an account that did not exist, and it read healthy
        for weeks on the other channel.
        """
        from mower_sdk.api import MowerAPI
        from mower_sdk.errors import MowerAPIError

        token = (data.get("token") or {}).get("access_token")
        if not token:
            return self.async_abort(reason="oauth_error")

        api = MowerAPI(
            session=async_get_clientsession(self.hass),
            token=token,
            base_url=API_BASE_URL,
        )
        try:
            devices = await api.async_get_devices()
        except MowerAPIError as err:
            _LOGGER.error("Navimow rejected the new token on authList: %s", err)
            return self.async_abort(reason="cannot_connect")
        except Exception:  # noqa: BLE001 -- vendor SDK raises broadly
            _LOGGER.exception("Navimow device list failed during setup")
            return self.async_abort(reason="cannot_connect")

        if not devices:
            # Not cannot_connect: the call succeeded and the answer was empty.
            # Collapsing the two would send the user to check their network
            # over an account that simply has no mower on it.
            return self.async_abort(reason="no_devices")

        if entry_id := self._reauth_entry_id():
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_create_entry(title="Navimow", data=data)

    def _reauth_entry_id(self) -> str | None:
        if self.source != "reauth":
            return None
        return self.context.get("entry_id")
