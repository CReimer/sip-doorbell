"""SIP Doorbell integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS, event_unique_id
from .sip import SIPDoorbellClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SIP Doorbell from a config entry."""
    registry = er.async_get(hass)
    if legacy_entity_id := registry.async_get_entity_id(
        "event", DOMAIN, "sip_hass_doorbell"
    ):
        legacy_entry = registry.async_get(legacy_entity_id)
        if legacy_entry is not None and legacy_entry.config_entry_id == entry.entry_id:
            registry.async_update_entity(
                legacy_entity_id, new_unique_id=event_unique_id(entry.entry_id)
            )
    client = SIPDoorbellClient(hass, entry)
    try:
        await client.async_start()
    except OSError as err:
        await client.async_stop()
        raise ConfigEntryNotReady(f"SIP endpoint is not ready: {err}") from err

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = client
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        domain_data.pop(entry.entry_id, None)
        if not domain_data:
            hass.data.pop(DOMAIN, None)
        await client.async_stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SIP Doorbell config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    client: SIPDoorbellClient = hass.data[DOMAIN].pop(entry.entry_id)
    await client.async_stop()
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return True
