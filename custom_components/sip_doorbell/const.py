"""Constants for the SIP Doorbell integration."""

from datetime import timedelta

DOMAIN = "sip_doorbell"
PLATFORMS = ["event"]

CONF_REGISTRAR = "registrar"
CONF_REGISTRAR_PORT = "registrar_port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_LISTEN_PORT = "listen_port"
CONF_DEVICE_NAME = "device_name"

DEFAULT_REGISTRAR = "fritz.box"
DEFAULT_REGISTRAR_PORT = 5060
DEFAULT_LISTEN_PORT = 5062
DEFAULT_DEVICE_NAME = "SIP HASS"

REGISTER_EXPIRES = timedelta(minutes=5)
REGISTER_RETRY = timedelta(seconds=30)
CALL_TIMEOUT = timedelta(minutes=2)

UNIQUE_ID = "sip_hass_doorbell"


def event_unique_id(entry_id: str) -> str:
    """Return an entry-scoped event unique ID."""
    return f"{UNIQUE_ID}_{entry_id}"
