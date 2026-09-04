"""Protocol-level tests that do not require Home Assistant."""

from __future__ import annotations

import asyncio
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryNotReady

import custom_components.sip_doorbell as integration_module
from custom_components.sip_doorbell import sip
from custom_components.sip_doorbell.const import (
    CONF_REGISTRAR,
    CONF_REGISTRAR_PORT,
    event_unique_id,
)

INVITE = (
    "INVITE sip:sip-hass@192.0.2.10:5062 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 192.0.2.1:5060;branch=z9hG4bK-a\r\n"
    "Via: SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-b\r\n"
    'From: "Door" <sip:door@fritz.box>;tag=from-tag\r\n'
    "To: <sip:sip-hass@fritz.box>\r\n"
    "Call-ID: test-call\r\n"
    "CSeq: 1 INVITE\r\n"
    "User-Agent: FRITZ!Box\r\n"
    "Content-Length: 0\r\n\r\n"
)


class ProtocolTests(unittest.TestCase):
    def test_parse_invite_and_compact_headers(self) -> None:
        message = sip.parse_message(INVITE)
        self.assertEqual(message.method, "INVITE")
        self.assertEqual(message.header("Call-ID"), "test-call")
        self.assertEqual(len(message.headers_for("Via")), 2)
        self.assertEqual(
            sip._party(message.header("From")), ("Door", "sip:door@fritz.box", "door")
        )

    def test_ringing_response_preserves_transaction_headers(self) -> None:
        message = sip.parse_message(INVITE)
        response = sip._build_response(
            message, 180, "Ringing", "192.0.2.10:5062", "to-tag"
        ).decode()
        self.assertIn("SIP/2.0 180 Ringing", response)
        self.assertEqual(response.count("Via:"), 2)
        self.assertIn("To: <sip:sip-hass@fritz.box>;tag=to-tag", response)
        self.assertIn("Call-ID: test-call", response)

    def test_digest_matches_rfc_example(self) -> None:
        challenge = (
            'Digest realm="testrealm@host.com", '
            'qop="auth,auth-int", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
            'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
        )
        authorization = sip.digest_authorization(
            challenge, "Mufasa", "Circle Of Life", "GET", "/dir/index.html"
        )
        self.assertIn('username="Mufasa"', authorization)
        self.assertIn("qop=auth", authorization)
        self.assertRegex(authorization, r'response="[0-9a-f]{32}"')

    def test_parse_response(self) -> None:
        message = sip.parse_message(
            "SIP/2.0 401 Unauthorized\r\nCSeq: 2 REGISTER\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertEqual(message.status, 401)
        self.assertTrue(re.match(r"2 REGISTER", message.header("CSeq")))

    def test_event_unique_id_is_scoped_to_config_entry(self) -> None:
        self.assertNotEqual(event_unique_id("entry-a"), event_unique_id("entry-b"))


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invite_emits_ring_without_answering_call(self) -> None:
        client = sip.SIPDoorbellClient.__new__(sip.SIPDoorbellClient)
        client._transport = MagicMock()
        client._contact = "192.0.2.10:5062"
        client._calls = {}
        events = []
        client._ring_listeners = [events.append]

        await client._handle_request(sip.parse_message(INVITE), ("192.0.2.1", 5060))

        responses = [
            call.args[0].decode() for call in client._transport.sendto.call_args_list
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["caller_user"], "door")
        self.assertIn("test-call", client._calls)
        self.assertTrue(
            any(response.startswith("SIP/2.0 100 ") for response in responses)
        )
        self.assertTrue(
            any(response.startswith("SIP/2.0 180 ") for response in responses)
        )
        self.assertFalse(
            any(response.startswith("SIP/2.0 200 ") for response in responses)
        )
        client._calls["test-call"].timeout.cancel()

    async def test_proxy_challenge_uses_proxy_authorization(self) -> None:
        client = sip.SIPDoorbellClient.__new__(sip.SIPDoorbellClient)
        client.entry = SimpleNamespace(
            data={
                CONF_USERNAME: "sip-hass",
                CONF_PASSWORD: "secret",
                CONF_REGISTRAR: "fritz.box",
                CONF_REGISTRAR_PORT: 5060,
            }
        )
        client._contact = "192.0.2.10:5062"
        client._sequence = 10
        challenged = sip.SIPMessage(
            status=407,
            headers={
                "Proxy-Authenticate": [
                    'Digest realm="fritz.box", nonce="nonce", qop="auth"'
                ]
            },
        )
        registered = sip.SIPMessage(status=200)
        client._send_register = AsyncMock(side_effect=[challenged, registered])

        await client._register_once()

        authorization = client._send_register.await_args_list[1].args[-1]
        self.assertEqual(authorization[0], "Proxy-Authorization")
        self.assertTrue(authorization[1].startswith("Digest "))

    async def test_registration_response_must_come_from_registrar(self) -> None:
        client = sip.SIPDoorbellClient.__new__(sip.SIPDoorbellClient)
        client._registrar = ("192.0.2.1", 5060)
        future = asyncio.get_running_loop().create_future()
        client._pending = {("REGISTER", 2): future}
        response = b"SIP/2.0 200 OK\r\nCSeq: 2 REGISTER\r\nContent-Length: 0\r\n\r\n"

        client._datagram_received(response, ("192.0.2.2", 5060))
        self.assertFalse(future.done())

        client._datagram_received(response, client._registrar)
        self.assertTrue(future.done())


class SetupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Ensure partial SIP setup cannot retain its UDP client."""

    async def test_platform_setup_failure_stops_client_and_clears_runtime(self) -> None:
        client = SimpleNamespace(
            async_start=AsyncMock(),
            async_stop=AsyncMock(),
        )
        hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(
                    side_effect=RuntimeError("event platform failed")
                )
            ),
        )
        entry = SimpleNamespace(entry_id="entry")
        registry = SimpleNamespace(async_get_entity_id=lambda *_args: None)

        with (
            patch.object(integration_module.er, "async_get", return_value=registry),
            patch.object(integration_module, "SIPDoorbellClient", return_value=client),
            self.assertRaisesRegex(RuntimeError, "event platform failed"),
        ):
            await integration_module.async_setup_entry(hass, entry)

        client.async_stop.assert_awaited_once()
        self.assertNotIn(integration_module.DOMAIN, hass.data)

    async def test_startup_oserror_is_cleaned_up_and_retried(self) -> None:
        client = SimpleNamespace(
            async_start=AsyncMock(side_effect=OSError("network unavailable")),
            async_stop=AsyncMock(),
        )
        hass = SimpleNamespace(data={})
        entry = SimpleNamespace(entry_id="entry")
        registry = SimpleNamespace(async_get_entity_id=lambda *_args: None)

        with (
            patch.object(integration_module.er, "async_get", return_value=registry),
            patch.object(integration_module, "SIPDoorbellClient", return_value=client),
            self.assertRaises(ConfigEntryNotReady),
        ):
            await integration_module.async_setup_entry(hass, entry)

        client.async_stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
