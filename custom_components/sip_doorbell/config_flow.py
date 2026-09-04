"""Config flow for SIP Doorbell."""

from __future__ import annotations

import socket
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEVICE_NAME,
    CONF_LISTEN_PORT,
    CONF_REGISTRAR,
    CONF_REGISTRAR_PORT,
    DEFAULT_DEVICE_NAME,
    DEFAULT_LISTEN_PORT,
    DEFAULT_REGISTRAR,
    DEFAULT_REGISTRAR_PORT,
    DOMAIN,
)


class SIPDoorbellConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a SIP Doorbell config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_REGISTRAR]}:{user_input[CONF_REGISTRAR_PORT]}:"
                f"{user_input[CONF_USERNAME]}"
            )
            self._abort_if_unique_id_configured()
            try:
                await self.hass.async_add_executor_job(
                    socket.getaddrinfo,
                    user_input[CONF_REGISTRAR],
                    user_input[CONF_REGISTRAR_PORT],
                    socket.AF_UNSPEC,
                    socket.SOCK_DGRAM,
                )
            except OSError:
                errors["base"] = "cannot_resolve"
            else:
                if any(
                    configured.data.get(CONF_LISTEN_PORT)
                    == user_input[CONF_LISTEN_PORT]
                    for configured in self._async_current_entries()
                ):
                    errors["base"] = "listen_port_in_use"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_DEVICE_NAME], data=user_input
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_REGISTRAR, default=DEFAULT_REGISTRAR): str,
                vol.Required(
                    CONF_REGISTRAR_PORT, default=DEFAULT_REGISTRAR_PORT
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_LISTEN_PORT, default=DEFAULT_LISTEN_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1024, max=65535)
                ),
                vol.Required(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
