# SIP Doorbell for Home Assistant

An unofficial Home Assistant custom integration that registers a local SIP
account and turns incoming SIP calls into native doorbell events.

## Features

- registers a dedicated SIP account with digest authentication;
- supports `401` and `407` registration challenges using MD5 or MD5-sess;
- exposes a Home Assistant `event` entity with the `doorbell` device class;
- reports caller, callee, user-agent and call metadata with every ring event;
- answers SIP `OPTIONS`, `CANCEL` and `BYE` signaling correctly;
- accepts multiple independently configured accounts on separate UDP ports;
- redacts SIP username and password from Home Assistant diagnostics;
- communicates locally without a cloud service.

The integration is verified with a FRITZ!Box registrar and Home Assistant
Container 2026.9.0. Registration, an incoming doorbell call and the resulting
Home Assistant event have been confirmed on that installation.

## Scope and limitations

SIP Doorbell is an **event-only signaling endpoint**. It sends `100 Trying` and
`180 Ringing` for an incoming call, then emits a Home Assistant event. It does
not answer the call with `200 OK`, negotiate SDP, transmit or receive RTP audio,
send DTMF or open a door. The caller normally ends the attempt, or the
integration expires it after two minutes.

It is designed for a doorbell, intercom or automation that can call a dedicated
SIP extension. Other standards-compliant registrars may work, but the currently
verified registrar is a FRITZ!Box.

## Requirements

- Home Assistant 2026.8.0 or newer;
- a reachable local SIP registrar;
- a dedicated SIP username and password;
- one inbound UDP port for each configured account.

For Home Assistant Container, use host networking or publish the configured UDP
port. Restrict that port to the registrar with the host firewall whenever
possible. The integration binds the port on all IPv4 interfaces, but ignores
SIP requests and responses that do not originate from the resolved registrar.

## Installation with HACS

Until the repository is included in HACS by default, add it as a custom
repository:

1. Open HACS in Home Assistant.
2. Select **Integrations**.
3. Open the menu and select **Custom repositories**.
4. Add `https://github.com/CReimer/sip-doorbell` as an **Integration**.
5. Install **SIP Doorbell** and restart Home Assistant.
6. Go to **Settings > Devices & services > Add integration** and select
   **SIP Doorbell**.

For manual installation, copy `custom_components/sip_doorbell` into the
`custom_components` directory of the Home Assistant configuration and restart
Home Assistant.

## FRITZ!Box setup

1. Create a dedicated IP telephone in **Telephony > Telephony Devices**.
2. Give it a unique SIP username and a strong password.
3. Configure the integration with `fritz.box`, registrar port `5060` and a free
   local UDP listen port such as `5062`.
4. Configure the doorbell or calling device to call that IP telephone.
5. Use the resulting `event.sip_hass_doorbell` entity as an automation trigger.

Existing installations may retain a different entity ID. Automations should
always reference the entity created by their own config entry.

## Event data and privacy

Each ring event can include caller and callee display names, SIP URIs and users,
the Call-ID, user-agent and timestamp. Treat these attributes as private. Before
posting diagnostics or logs, redact telephone numbers, usernames, addresses,
Call-IDs and all credentials.

## Development

Run the tests against the pinned Home Assistant release:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -t .
```

The integration was developed with substantial assistance from generative AI.
All changes are reviewed, tested and released under the responsibility of the
maintainer.

## License

The source code and original project icon are licensed under the
[Apache License 2.0](LICENSE) (`Apache-2.0`).

SIP, FRITZ!Box and other names or marks belong to their respective owners. They
are used only to describe interoperability. This project is independent and is
not affiliated with or endorsed by AVM or Home Assistant.

## Tests and coverage

Use Python 3.14 and the pinned Home Assistant test dependencies:

```bash
python -m pip install -r requirements-test.txt
python tools/run_tests.py
```

The suite mocks device and external service access; Blink also exercises a local
TCP relay. Every integration Python module is included in coverage, including
modules not imported by tests. The test command and GitHub Actions both require
at least **91% line coverage and 91% branch coverage**, checked separately without
rounding. HTML, XML and JSON reports are written to `coverage-report/` and uploaded
as the `coverage` artifact by CI.
