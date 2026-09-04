"""Event entity for SIP Doorbell."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_DEVICE_NAME, DOMAIN, event_unique_id
from .sip import SIPDoorbellClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the doorbell event entity."""
    async_add_entities([SIPDoorbellEvent(entry, hass.data[DOMAIN][entry.entry_id])])


class SIPDoorbellEvent(EventEntity):
    """Represent incoming SIP doorbell calls."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types: ClassVar[list[str]] = ["ring"]
    _attr_has_entity_name = True
    _attr_name = "Doorbell"
    _attr_suggested_object_id = "sip_hass_doorbell"

    def __init__(self, entry: ConfigEntry, client: SIPDoorbellClient) -> None:
        self._entry = entry
        self._client = client
        self._attr_unique_id = event_unique_id(entry.entry_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "manufacturer": "SIP",
            "model": "FRITZ!Box SIP doorbell",
            "name": entry.data[CONF_DEVICE_NAME],
        }
        self._remove_listeners: list[Callable[[], None]] = []

    @property
    def available(self) -> bool:
        """Return whether SIP registration is active."""
        return self._client.registered

    async def async_added_to_hass(self) -> None:
        """Subscribe to client events."""
        await super().async_added_to_hass()
        self._remove_listeners.append(self._client.add_ring_listener(self._handle_ring))
        self._remove_listeners.append(
            self._client.add_availability_listener(self._handle_availability)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from client events."""
        for remove in self._remove_listeners:
            remove()
        self._remove_listeners.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_ring(self, attributes: dict[str, Any]) -> None:
        self._trigger_event("ring", attributes)
        self.async_write_ha_state()

    @callback
    def _handle_availability(self) -> None:
        self.async_write_ha_state()
