"""SIP call state, transport cleanup and registration failure contracts."""

import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from custom_components.sip_doorbell import sip
from custom_components.sip_doorbell.const import (
    CONF_LISTEN_PORT,
    CONF_REGISTRAR,
    CONF_REGISTRAR_PORT,
    REGISTER_EXPIRES,
    REGISTER_RETRY,
)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tasks = []

        def spawn(coro, name):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        self.hass = NS(async_create_background_task=spawn)
        self.entry = NS(
            data={
                CONF_REGISTRAR: "registrar",
                CONF_REGISTRAR_PORT: 5060,
                CONF_LISTEN_PORT: 5061,
                "username": "user",
                "password": "secret",
            }
        )
        self.client = sip.SIPDoorbellClient(self.hass, self.entry)
        self.client._registrar = ("192.0.2.1", 5060)
        self.transport = Mock()
        self.client._transport = self.transport
        self.client._contact = "192.0.2.2:5061"

    async def asyncTearDown(self):
        await self.client.async_stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def request(self, method="INVITE", call="call"):
        return sip.parse_message(
            f'{method} sip:user@registrar SIP/2.0\r\nCall-ID: {call}\r\nFrom: "Door" <sip:door@registrar>\r\nTo: sip:user@registrar\r\nCSeq: 1 {method}\r\n\r\n'
        )

    async def test_invite_retransmission_and_cancel(self):
        ring = Mock()
        remove = self.client.add_ring_listener(ring)
        for _ in range(2):
            await self.client._handle_request(self.request(), self.client._registrar)
        self.assertEqual(self.client.active_call_count, 1)
        ring.assert_called_once()
        self.assertEqual(ring.call_args.args[0]["caller_user"], "door")
        self.assertEqual(ring.call_args.args[0]["callee_user"], "user")
        replies = [
            sip.parse_message(c.args[0]) for c in self.transport.sendto.call_args_list
        ]
        self.assertEqual([r.status for r in replies], [100, 180, 100, 180])
        self.assertEqual(replies[1].header("To"), replies[3].header("To"))
        timer = self.client._calls["call"].timeout
        await self.client._handle_request(
            self.request("CANCEL"), self.client._registrar
        )
        self.assertTrue(timer.cancelled())
        self.assertEqual(self.client.active_call_count, 0)
        self.assertEqual(
            sip.parse_message(self.transport.sendto.call_args.args[0]).status, 487
        )
        await self.client._handle_request(
            self.request("CANCEL"), self.client._registrar
        )
        self.assertEqual(
            sip.parse_message(self.transport.sendto.call_args.args[0]).status, 200
        )
        remove()
        self.assertEqual(self.client._ring_listeners, [])

    async def test_bye_options_unsupported_and_expiry(self):
        await self.client._handle_request(self.request(), self.client._registrar)
        timer = self.client._calls["call"].timeout
        await self.client._handle_request(self.request("BYE"), self.client._registrar)
        self.assertTrue(timer.cancelled())
        for method, status in [("BYE", 200), ("OPTIONS", 200), ("MESSAGE", 501)]:
            await self.client._handle_request(
                self.request(method), self.client._registrar
            )
            self.assertEqual(
                sip.parse_message(self.transport.sendto.call_args.args[0]).status,
                status,
            )
        await self.client._handle_request(self.request(), self.client._registrar)
        self.client._calls["call"].timeout.cancel()
        self.client._expire_call("call")
        self.assertEqual(self.client.active_call_count, 0)
        self.assertEqual(
            sip.parse_message(self.transport.sendto.call_args.args[0]).status, 487
        )
        count = self.transport.sendto.call_count
        self.client._expire_call("missing")
        self.client._transport = None
        await self.client._handle_request(self.request(), self.client._registrar)
        self.assertEqual(self.transport.sendto.call_count, count)

    async def test_start_binds_and_stop_cancels_all_work(self):
        loop = asyncio.get_running_loop()
        route = Mock()
        route.getsockname.return_value = ("192.0.2.2", 1234)

        async def endpoint(factory, **kw):
            self.assertIsInstance(factory(), sip._SIPProtocol)
            self.assertEqual(kw["local_addr"], ("0.0.0.0", 5061))
            return self.transport, None

        with (
            patch.object(
                loop,
                "getaddrinfo",
                AsyncMock(
                    return_value=[(None, None, None, None, self.client._registrar)]
                ),
            ),
            patch.object(loop, "create_datagram_endpoint", endpoint),
            patch.object(sip.socket, "socket", return_value=route),
            patch.object(self.client, "_registration_loop", AsyncMock()),
        ):
            await self.client.async_start()
        self.assertEqual(self.client._contact, "192.0.2.2:5061")
        route.close.assert_called_once()
        future = loop.create_future()
        self.client._pending[("REGISTER", 1)] = future
        await self.client._handle_request(self.request(), self.client._registrar)
        timer = self.client._calls["call"].timeout
        listener = Mock()
        remove = self.client.add_availability_listener(listener)
        self.client._set_registered(True)
        self.client._set_registered(True)
        listener.assert_called_once()
        await self.client.async_stop()
        self.assertFalse(self.client.registered)
        self.assertEqual(listener.call_count, 2)
        self.assertTrue(future.cancelled())
        self.assertTrue(timer.cancelled())
        self.assertIsNone(self.client._registration_task)
        self.transport.close.assert_called_once()
        remove()
        self.assertEqual(self.client._availability_listeners, [])

    async def test_start_failure_closes_transport(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=OSError("dns"))):
            with self.assertRaisesRegex(OSError, "dns"):
                await self.client.async_start()
        self.assertIsNone(self.client._transport)
        self.transport.close.assert_called_once()

    async def test_registration_loop_success_failure_and_cancel(self):
        for failure in (
            None,
            TimeoutError("timeout"),
            OSError("udp"),
            ValueError("digest"),
        ):
            with self.subTest(failure=failure):
                self.client._registered = True
                with (
                    patch.object(
                        self.client, "_register_once", AsyncMock(side_effect=failure)
                    ),
                    patch.object(
                        sip.asyncio,
                        "sleep",
                        AsyncMock(side_effect=asyncio.CancelledError),
                    ) as sleep,
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.client._registration_loop()
                    self.assertEqual(self.client.registered, failure is None)
                    sleep.assert_awaited_once_with(
                        REGISTER_EXPIRES.total_seconds() * 0.8
                        if failure is None
                        else REGISTER_RETRY.total_seconds()
                    )
        with patch.object(
            self.client, "_register_once", AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.client._registration_loop()

    async def test_registration_retries_after_failure(self):
        with (
            patch.object(
                self.client,
                "_register_once",
                AsyncMock(side_effect=[OSError("retry"), None]),
            ) as register,
            patch.object(
                sip.asyncio,
                "sleep",
                AsyncMock(side_effect=[None, asyncio.CancelledError]),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.client._registration_loop()
        self.assertEqual(register.await_count, 2)
        self.assertTrue(self.client.registered)

    async def test_register_status_and_authorization(self):
        self.entry.data[CONF_REGISTRAR_PORT] = 5070
        challenge = sip.parse_message(
            'SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="r", nonce="n"\r\n\r\n'
        )
        for statuses, error in [
            ([sip.SIPMessage(status=200)], False),
            ([sip.SIPMessage(status=500)], True),
            ([challenge, sip.SIPMessage(status=403)], True),
            ([challenge, sip.SIPMessage(status=200)], False),
        ]:
            with patch.object(
                self.client, "_send_register", AsyncMock(side_effect=statuses)
            ) as send:
                if error:
                    with self.assertRaises(OSError):
                        await self.client._register_once()
                else:
                    await self.client._register_once()
                self.assertEqual(send.call_args_list[0].args[2], "sip:registrar:5070")
                if len(statuses) == 2:
                    self.assertEqual(send.call_args.args[-1][0], "Authorization")
        with patch.object(
            self.client,
            "_send_and_wait",
            AsyncMock(return_value=sip.SIPMessage(status=200)),
        ) as send:
            for auth in (None, ("Authorization", "Digest test")):
                await self.client._send_register(
                    42, "user", "sip:registrar", "call", "tag", auth
                )
                packet = sip.parse_message(send.call_args.args[0])
                self.assertEqual(packet.header("CSeq"), "42 REGISTER")
                self.assertEqual(
                    packet.header("Authorization"), "Digest test" if auth else ""
                )

    async def test_pending_reply_timeout_and_not_ready(self):
        async def respond(future, timeout):
            self.client._datagram_received(
                b"SIP/2.0 200 OK\r\nCSeq: 42 REGISTER\r\n\r\n", self.client._registrar
            )
            return await future

        with patch.object(sip.asyncio, "wait_for", respond):
            self.assertEqual(
                (await self.client._send_and_wait(b"packet", "REGISTER", 42)).status,
                200,
            )
        self.assertEqual(self.client._pending, {})
        with patch.object(sip.asyncio, "wait_for", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await self.client._send_and_wait(b"packet", "REGISTER", 42)
        self.assertEqual(self.client._pending, {})
        self.client._transport = None
        with self.assertRaises(OSError):
            await self.client._send_and_wait(b"packet", "REGISTER", 42)

    async def test_datagram_filtering_and_protocol_callbacks(self):
        protocol = sip._SIPProtocol(self.client)
        protocol.connection_made(self.transport)
        with self.assertLogs(sip.__name__, "WARNING"):
            protocol.error_received(OSError("udp"))
        with patch.object(self.client, "_handle_request", AsyncMock()) as handle:
            for data, remote in [
                (b"", self.client._registrar),
                (b"OPTIONS x SIP/2.0", ("192.0.2.8", 5060)),
                (b"SIP/2.0 200 OK", self.client._registrar),
                (b"SIP/2.0 200 OK\r\nCSeq: 8 REGISTER", self.client._registrar),
            ]:
                protocol.datagram_received(data, remote)
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            self.client._pending[("REGISTER", 8)] = done
            protocol.datagram_received(
                b"SIP/2.0 200 OK\r\nCSeq: 8 REGISTER", self.client._registrar
            )
            handle.assert_not_awaited()
            protocol.datagram_received(b"OPTIONS x SIP/2.0", self.client._registrar)
            await asyncio.gather(*self.tasks)
            handle.assert_awaited_once()
            self.client._registrar = None
            protocol.datagram_received(b"OPTIONS x SIP/2.0", ("192.0.2.1", 5060))
            protocol.datagram_received(b"SIP/2.0 200 OK", ("192.0.2.1", 5060))


class ParserEdges(unittest.TestCase):
    def test_empty_malformed_and_digest_validation(self):
        self.assertEqual(sip.parse_message("").method, "")
        self.assertEqual(sip.parse_message(" \ninvalid header").method, "")
        self.assertEqual(sip.parse_message("SIP/2.0 nope\ninvalid header").status, 0)
        for challenge in (
            'Digest realm="r"',
            'Digest nonce="n"',
            'Digest realm="r", nonce="n", algorithm=SHA-512',
        ):
            with self.assertRaises(ValueError):
                sip.digest_authorization(challenge, "u", "p", "REGISTER", "sip:r")
        with patch.object(sip, "_token", return_value="c"):
            result = sip.digest_authorization(
                'Digest ignored, realm="r", nonce="n", algorithm=MD5-SESS, opaque="o"',
                "u",
                "p",
                "REGISTER",
                "sip:r",
            )
        self.assertIn('opaque="o"', result)
        self.assertIn("algorithm=MD5-SESS", result)
        self.assertNotIn("qop=", result)
        response = sip.parse_message(
            sip._build_response(
                sip.parse_message("OPTIONS x SIP/2.0\nTo: <sip:u@r>;tag=existing"),
                200,
                "OK",
                "local",
                "new",
            )
        )
        self.assertEqual(response.header("To"), "<sip:u@r>;tag=existing")
