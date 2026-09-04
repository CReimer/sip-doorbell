"""Diagnostics for SIP Doorbell."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .sip import SIPDoorbellClient


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    client: SIPDoorbellClient = hass.data[DOMAIN][entry.entry_id]
    data = dict(entry.data)
    data[CONF_USERNAME] = "REDACTED"
    data[CONF_PASSWORD] = "REDACTED"
    return {
        "entry": data,
        "registered": client.registered,
        "active_calls": client.active_call_count,
    }
