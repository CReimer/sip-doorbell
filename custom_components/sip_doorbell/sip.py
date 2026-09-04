"""Small asynchronous SIP/UDP client used by SIP Doorbell."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import socket
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .const import (
    CALL_TIMEOUT,
    CONF_LISTEN_PORT,
    CONF_REGISTRAR,
    CONF_REGISTRAR_PORT,
    REGISTER_EXPIRES,
    REGISTER_RETRY,
)

_LOGGER = logging.getLogger(__name__)


def _token(length: int = 16) -> str:
    return "".join(random.SystemRandom().choices(string.hexdigits.lower(), k=length))


def _canonical_header(name: str) -> str:
    compact = {
        "i": "Call-ID",
        "f": "From",
        "t": "To",
        "v": "Via",
        "m": "Contact",
        "l": "Content-Length",
    }
    lowered = name.strip().lower()
    if lowered in compact:
        return compact[lowered]
    return "-".join(part.capitalize() for part in lowered.split("-"))


@dataclass(slots=True)
class SIPMessage:
    """Parsed SIP message."""

    start_line: str = ""
    method: str = ""
    status: int = 0
    headers: dict[str, list[str]] = field(default_factory=dict)
    body: str = ""

    def header(self, name: str) -> str:
        values = self.headers.get(_canonical_header(name), [])
        return values[0] if values else ""

    def headers_for(self, name: str) -> list[str]:
        return self.headers.get(_canonical_header(name), [])


def parse_message(raw: bytes | str) -> SIPMessage:
    """Parse a SIP message without interpreting its SDP body."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    head, separator, body = text.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    message = SIPMessage(body=body if separator else "")
    if not lines:
        return message
    message.start_line = lines[0].strip()
    start = message.start_line.split()
    if start and start[0].upper().startswith("SIP/2.0") and len(start) > 1:
        try:
            message.status = int(start[1])
        except ValueError:
            pass
    elif start:
        message.method = start[0].upper()
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        message.headers.setdefault(_canonical_header(name), []).append(value.strip())
    return message


def _split_digest_parameters(value: str) -> dict[str, str]:
    value = re.sub(r"^\s*Digest\s+", "", value, flags=re.IGNORECASE)
    parts = re.findall(r'(?:[^,"]|"[^"]*")+', value)
    result: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, item = part.split("=", 1)
        result[key.strip().lower()] = item.strip().strip('"')
    return result


def digest_authorization(
    challenge: str,
    username: str,
    password: str,
    method: str,
    uri: str,
    nonce_count: int = 1,
) -> str:
    """Build RFC 2617/7616 MD5 digest authorization used by FRITZ!Box."""
    values = _split_digest_parameters(challenge)
    realm = values.get("realm", "")
    nonce = values.get("nonce", "")
    if not realm or not nonce:
        raise ValueError("digest challenge has no realm or nonce")
    algorithm = values.get("algorithm", "MD5")
    if algorithm.upper() not in {"MD5", "MD5-SESS"}:
        raise ValueError(f"unsupported digest algorithm {algorithm}")
    qop_options = [item.strip() for item in values.get("qop", "").split(",")]
    qop = "auth" if "auth" in qop_options else ""
    cnonce = _token()
    nc = f"{nonce_count:08x}"

    def md5(value: str) -> str:
        return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()

    ha1 = md5(f"{username}:{realm}:{password}")
    if algorithm.upper() == "MD5-SESS":
        ha1 = md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = md5(f"{method}:{uri}")
    response = (
        md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        if qop
        else md5(f"{ha1}:{nonce}:{ha2}")
    )
    fields = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm}",
    ]
    if opaque := values.get("opaque"):
        fields.append(f'opaque="{opaque}"')
    if qop:
        fields.extend((f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'))
    return "Digest " + ", ".join(fields)


def _party(value: str) -> tuple[str, str, str]:
    display_name = ""
    uri = ""
    if "<" in value:
        display_name, remainder = value.split("<", 1)
        uri = remainder.split(">", 1)[0]
        display_name = display_name.strip().strip('"')
    else:
        uri = value.split(";", 1)[0].strip()
    uri = uri.split(";", 1)[0].strip()
    address = uri.removeprefix("sip:")
    user = address.split("@", 1)[0]
    return display_name, uri, user


def _build_response(
    request: SIPMessage,
    status: int,
    reason: str,
    contact: str,
    to_tag: str | None = None,
) -> bytes:
    lines = [f"SIP/2.0 {status} {reason}"]
    lines.extend(f"Via: {via}" for via in request.headers_for("Via"))
    for header in ("From", "To", "Call-ID", "CSeq"):
        if not (value := request.header(header)):
            continue
        if header == "To" and to_tag and ";tag=" not in value.lower():
            value += f";tag={to_tag}"
        lines.append(f"{header}: {value}")
    if status in {180, 200}:
        lines.append(f"Contact: <sip:sip-hass@{contact}>")
    lines.extend(("User-Agent: Home Assistant SIP Doorbell", "Content-Length: 0"))
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


@dataclass(slots=True)
class _ActiveCall:
    invite: SIPMessage
    remote: tuple[str, int]
    to_tag: str
    timeout: asyncio.TimerHandle


class _SIPProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: SIPDoorbellClient) -> None:
        self.client = client

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.client._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client._datagram_received(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("SIP UDP transport error: %s", exc)


class SIPDoorbellClient:
    """Register a SIP user and turn incoming INVITEs into callbacks."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._transport: asyncio.DatagramTransport | None = None
        self._registrar: tuple[str, int] | None = None
        self._contact = ""
        self._registration_task: asyncio.Task[None] | None = None
        self._pending: dict[tuple[str, int], asyncio.Future[SIPMessage]] = {}
        self._calls: dict[str, _ActiveCall] = {}
        self._ring_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._availability_listeners: list[Callable[[], None]] = []
        self._registered = False
        self._sequence = random.SystemRandom().randint(1, 10000)

    @property
    def registered(self) -> bool:
        return self._registered

    @property
    def active_call_count(self) -> int:
        return len(self._calls)

    async def async_start(self) -> None:
        """Bind the UDP listener and start registration."""
        try:
            loop = asyncio.get_running_loop()
            addresses = await loop.getaddrinfo(
                self.entry.data[CONF_REGISTRAR],
                self.entry.data[CONF_REGISTRAR_PORT],
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )
            self._registrar = addresses[0][4]
            listen_port = self.entry.data[CONF_LISTEN_PORT]
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _SIPProtocol(self),
                local_addr=("0.0.0.0", listen_port),
                family=socket.AF_INET,
            )
            self._transport = transport
            route_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                route_socket.connect(self._registrar)
                source_ip = route_socket.getsockname()[0]
            finally:
                route_socket.close()
            self._contact = f"{source_ip}:{listen_port}"
            self._registration_task = self.hass.async_create_background_task(
                self._registration_loop(), "sip_doorbell_registration"
            )
        except BaseException:
            await self.async_stop()
            raise

    async def async_stop(self) -> None:
        """Stop registration, calls and UDP transport."""
        if self._registration_task:
            self._registration_task.cancel()
            await asyncio.gather(self._registration_task, return_exceptions=True)
            self._registration_task = None
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        for call in self._calls.values():
            call.timeout.cancel()
        self._calls.clear()
        if self._transport:
            self._transport.close()
            self._transport = None
        self._set_registered(False)

    @callback
    def add_ring_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        self._ring_listeners.append(listener)
        return lambda: self._ring_listeners.remove(listener)

    @callback
    def add_availability_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    @callback
    def _set_registered(self, registered: bool) -> None:
        if self._registered == registered:
            return
        self._registered = registered
        for listener in self._availability_listeners:
            listener()

    async def _registration_loop(self) -> None:
        while True:
            try:
                await self._register_once()
            except asyncio.CancelledError:
                raise
            except (TimeoutError, OSError, ValueError) as err:
                self._set_registered(False)
                _LOGGER.warning("SIP registration failed: %s", err)
                await asyncio.sleep(REGISTER_RETRY.total_seconds())
                continue
            self._set_registered(True)
            _LOGGER.info("SIP registration successful")
            await asyncio.sleep(REGISTER_EXPIRES.total_seconds() * 0.8)

    async def _register_once(self) -> None:
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]
        host = self.entry.data[CONF_REGISTRAR]
        port = self.entry.data[CONF_REGISTRAR_PORT]
        uri = f"sip:{host}" if port == 5060 else f"sip:{host}:{port}"
        call_id = f"{_token()}@{self._contact.split(':', 1)[0]}"
        from_tag = _token(12)
        sequence = self._next_sequence()
        response = await self._send_register(
            sequence, username, uri, call_id, from_tag, None
        )
        if 200 <= response.status < 300:
            return
        if response.status not in {401, 407}:
            raise OSError(f"registrar returned SIP {response.status}")
        proxy_auth = response.status == 407
        challenge = response.header(
            "Proxy-Authenticate" if proxy_auth else "WWW-Authenticate"
        )
        authorization = digest_authorization(
            challenge, username, password, "REGISTER", uri
        )
        response = await self._send_register(
            self._next_sequence(),
            username,
            uri,
            call_id,
            from_tag,
            (
                "Proxy-Authorization" if proxy_auth else "Authorization",
                authorization,
            ),
        )
        if not 200 <= response.status < 300:
            raise OSError(f"authenticated registration returned SIP {response.status}")

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    async def _send_register(
        self,
        sequence: int,
        username: str,
        uri: str,
        call_id: str,
        from_tag: str,
        authorization: tuple[str, str] | None,
    ) -> SIPMessage:
        host = self.entry.data[CONF_REGISTRAR]
        lines = [
            f"REGISTER {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self._contact};branch=z9hG4bK-{_token()};rport",
            "Max-Forwards: 70",
            f"From: <sip:{username}@{host}>;tag={from_tag}",
            f"To: <sip:{username}@{host}>",
            f"Call-ID: {call_id}",
            f"CSeq: {sequence} REGISTER",
            f"Contact: <sip:{username}@{self._contact}>",
            f"Expires: {int(REGISTER_EXPIRES.total_seconds())}",
            "User-Agent: Home Assistant SIP Doorbell",
        ]
        if authorization:
            lines.append(f"{authorization[0]}: {authorization[1]}")
        lines.append("Content-Length: 0")
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode()
        return await self._send_and_wait(payload, "REGISTER", sequence)

    async def _send_and_wait(
        self, payload: bytes, method: str, sequence: int
    ) -> SIPMessage:
        if not self._transport or not self._registrar:
            raise OSError("SIP transport is not ready")
        key = (method, sequence)
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        self._transport.sendto(payload, self._registrar)
        try:
            return await asyncio.wait_for(future, timeout=5)
        finally:
            self._pending.pop(key, None)

    @callback
    def _datagram_received(self, data: bytes, remote: tuple[str, int]) -> None:
        message = parse_message(data)
        if message.status:
            if not self._registrar or remote != self._registrar:
                _LOGGER.warning(
                    "Ignoring SIP response from unexpected endpoint %s:%s",
                    remote[0],
                    remote[1],
                )
                return
            match = re.match(r"(\d+)\s+([A-Za-z]+)", message.header("CSeq"))
            if (
                match
                and (future := self._pending.get((match[2].upper(), int(match[1]))))
                and not future.done()
            ):
                future.set_result(message)
            return
        if not message.method or not self._registrar:
            return
        if remote[0] != self._registrar[0]:
            _LOGGER.warning("Ignoring SIP request from unexpected host %s", remote[0])
            return
        self.hass.async_create_background_task(
            self._handle_request(message, remote),
            f"sip_doorbell_{message.method.lower()}",
        )

    async def _handle_request(
        self, message: SIPMessage, remote: tuple[str, int]
    ) -> None:
        if not self._transport:
            return
        method = message.method
        call_id = message.header("Call-ID")
        if method == "INVITE":
            to_tag = (
                self._calls[call_id].to_tag if call_id in self._calls else _token(12)
            )
            self._transport.sendto(
                _build_response(message, 100, "Trying", self._contact), remote
            )
            self._transport.sendto(
                _build_response(message, 180, "Ringing", self._contact, to_tag), remote
            )
            if call_id in self._calls:
                return
            timeout = asyncio.get_running_loop().call_later(
                CALL_TIMEOUT.total_seconds(), self._expire_call, call_id
            )
            self._calls[call_id] = _ActiveCall(message, remote, to_tag, timeout)
            caller_name, caller_uri, caller_user = _party(message.header("From"))
            callee_name, callee_uri, callee_user = _party(message.header("To"))
            attributes = {
                "call_id": call_id,
                "caller_display_name": caller_name,
                "caller_uri": caller_uri,
                "caller_user": caller_user,
                "callee_display_name": callee_name,
                "callee_uri": callee_uri,
                "callee_user": callee_user,
                "user_agent": message.header("User-Agent"),
                "occurred_at": datetime.now(UTC).isoformat(),
            }
            for listener in self._ring_listeners:
                listener(attributes)
            return
        if method == "CANCEL":
            self._transport.sendto(
                _build_response(message, 200, "OK", self._contact), remote
            )
            if call := self._calls.pop(call_id, None):
                call.timeout.cancel()
                self._transport.sendto(
                    _build_response(
                        call.invite,
                        487,
                        "Request Terminated",
                        self._contact,
                        call.to_tag,
                    ),
                    call.remote,
                )
            return
        if method == "BYE":
            self._transport.sendto(
                _build_response(message, 200, "OK", self._contact), remote
            )
            if call := self._calls.pop(call_id, None):
                call.timeout.cancel()
            return
        if method == "OPTIONS":
            self._transport.sendto(
                _build_response(message, 200, "OK", self._contact), remote
            )
            return
        self._transport.sendto(
            _build_response(message, 501, "Not Implemented", self._contact), remote
        )

    @callback
    def _expire_call(self, call_id: str) -> None:
        if not (call := self._calls.pop(call_id, None)) or not self._transport:
            return
        self._transport.sendto(
            _build_response(
                call.invite, 487, "Request Terminated", self._contact, call.to_tag
            ),
            call.remote,
        )
        _LOGGER.info("SIP call %s timed out", call_id)
