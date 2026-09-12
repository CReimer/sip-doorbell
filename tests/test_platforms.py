"""Configuration, migration, event subscription and redaction contracts."""

from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from custom_components import sip_doorbell as integration
from custom_components.sip_doorbell import config_flow, diagnostics, event
from custom_components.sip_doorbell.const import (
    CONF_DEVICE_NAME,
    CONF_LISTEN_PORT,
    CONF_REGISTRAR,
    CONF_REGISTRAR_PORT,
    DOMAIN,
    event_unique_id,
)


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = NS(
            entry_id="entry",
            data={
                CONF_DEVICE_NAME: "Door",
                CONF_REGISTRAR: "registrar",
                CONF_REGISTRAR_PORT: 5060,
                CONF_LISTEN_PORT: 5061,
                "username": "u",
                "password": "p",
            },
        )

    async def test_config_form_resolution_conflict_and_creation(self):
        flow = config_flow.SIPDoorbellConfigFlow()
        flow.hass = NS(async_add_executor_job=AsyncMock())
        self.assertEqual((await flow.async_step_user())["step_id"], "user")
        with (
            patch.object(flow, "async_set_unique_id", AsyncMock()) as unique,
            patch.object(flow, "_abort_if_unique_id_configured"),
            patch.object(flow, "_async_current_entries", return_value=[]) as current,
        ):
            result = await flow.async_step_user(self.entry.data)
            self.assertEqual(result["data"], self.entry.data)
            unique.assert_awaited_once_with("registrar:5060:u")
            current.return_value = [self.entry]
            self.assertEqual(
                (await flow.async_step_user(self.entry.data))["errors"],
                {"base": "listen_port_in_use"},
            )
            current.return_value = [NS(data={CONF_LISTEN_PORT: 5062})]
            self.assertEqual(
                (await flow.async_step_user(self.entry.data))["data"], self.entry.data
            )
            flow.hass.async_add_executor_job.side_effect = OSError("dns")
            self.assertEqual(
                (await flow.async_step_user(self.entry.data))["errors"],
                {"base": "cannot_resolve"},
            )

    async def test_setup_migrates_only_owned_legacy_entity(self):
        registry = Mock()
        client = NS(async_start=AsyncMock(), async_stop=AsyncMock())
        hass = NS(
            data={},
            config_entries=NS(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=False),
            ),
        )
        for old in (None, NS(config_entry_id="other"), NS(config_entry_id="entry")):
            registry.reset_mock()
            registry.async_get_entity_id.return_value = "event.old"
            registry.async_get.return_value = old
            with (
                patch.object(integration.er, "async_get", return_value=registry),
                patch.object(integration, "SIPDoorbellClient", return_value=client),
            ):
                self.assertTrue(await integration.async_setup_entry(hass, self.entry))
            self.assertEqual(
                registry.async_update_entity.call_count,
                int(old is not None and old.config_entry_id == "entry"),
            )
        self.assertFalse(await integration.async_unload_entry(hass, self.entry))
        client.async_stop.assert_not_awaited()
        hass.config_entries.async_unload_platforms.return_value = True
        hass.data[DOMAIN]["other"] = client
        self.assertTrue(await integration.async_unload_entry(hass, self.entry))
        self.assertIn("other", hass.data[DOMAIN])
        self.assertTrue(
            await integration.async_unload_entry(hass, NS(entry_id="other"))
        )
        self.assertNotIn(DOMAIN, hass.data)
        hass.data = {DOMAIN: {"other": client}}
        hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError(
            "platform"
        )
        with (
            patch.object(integration.er, "async_get", return_value=registry),
            patch.object(integration, "SIPDoorbellClient", return_value=client),
        ):
            with self.assertRaises(RuntimeError):
                await integration.async_setup_entry(hass, self.entry)
        self.assertEqual(hass.data[DOMAIN], {"other": client})

    async def test_event_listeners_and_redaction(self):
        remove_ring, remove_available = Mock(), Mock()
        client = NS(
            registered=True,
            active_call_count=2,
            add_ring_listener=Mock(return_value=remove_ring),
            add_availability_listener=Mock(return_value=remove_available),
        )
        hass = NS(data={DOMAIN: {"entry": client}})
        entities = []
        await event.async_setup_entry(hass, self.entry, entities.extend)
        entity = entities[0]
        self.assertEqual(entity.unique_id, event_unique_id("entry"))
        self.assertTrue(entity.available)
        client.registered = False
        self.assertFalse(entity.available)
        with (
            patch.object(event.EventEntity, "async_added_to_hass", AsyncMock()),
            patch.object(event.EventEntity, "async_will_remove_from_hass", AsyncMock()),
            patch.object(entity, "async_write_ha_state") as write,
        ):
            await entity.async_added_to_hass()
            client.add_ring_listener.call_args.args[0]({"caller_user": "door"})
            self.assertEqual(entity.state_attributes["event_type"], "ring")
            self.assertEqual(entity.state_attributes["caller_user"], "door")
            client.add_availability_listener.call_args.args[0]()
            self.assertEqual(write.call_count, 2)
            await entity.async_will_remove_from_hass()
            remove_ring.assert_called_once()
            remove_available.assert_called_once()
            self.assertEqual(entity._remove_listeners, [])
        result = await diagnostics.async_get_config_entry_diagnostics(hass, self.entry)
        self.assertEqual(result["entry"]["password"], "REDACTED")
        self.assertEqual(result["entry"]["username"], "REDACTED")
        self.assertEqual(self.entry.data["password"], "p")
        self.assertEqual(result["active_calls"], 2)
