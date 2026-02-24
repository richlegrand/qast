"""DLNA (UPnP AVTransport) casting via SOAP."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html import escape as html_escape

from .. import config
from ..discovery.types import Device
from ..log import get_logger

log = get_logger("cast.dlna")

_AVT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"

_MIME_TYPES = {
    "ts": "video/mpeg",
    "mp4": "video/mp4",
}


def _soap_action(control_url: str, action: str, args: str = "") -> bytes:
    """Send a SOAP request to a DLNA AVTransport service."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{_AVT_SERVICE}">'
        "<InstanceID>0</InstanceID>"
        f"{args}"
        f"</u:{action}>"
        "</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")

    log.debug("SOAP %s -> %s", action, control_url)

    req = urllib.request.Request(
        control_url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{_AVT_SERVICE}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        log.error("SOAP %s failed (HTTP %d): %s", action, e.code, error_body)
        raise


def _get_transport_state(control_url: str) -> str:
    """Query the current transport state of the DLNA renderer."""
    try:
        response = _soap_action(control_url, "GetTransportInfo")
        root = ET.fromstring(response)
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "CurrentTransportState" and elem.text:
                return elem.text
    except Exception:
        log.debug("GetTransportInfo failed", exc_info=True)
    return "UNKNOWN"


def play(device: Device, url: str, video_format: str = "mp4") -> None:
    """Cast a URL to a DLNA renderer via AVTransport SOAP."""
    escaped_url = html_escape(url)
    mime_type = _MIME_TYPES.get(video_format, "video/mp4")
    dlna_extras = config.DLNA_FLAGS
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        "<dc:title>qast</dc:title>"
        f'<res protocolInfo="http-get:*:{mime_type}:{dlna_extras}">{escaped_url}</res>'
        "<upnp:class>object.item.videoItem.videoBroadcast</upnp:class>"
        "</item>"
        "</DIDL-Lite>"
    )
    metadata = html_escape(didl)
    args = (
        f"<CurrentURI>{escaped_url}</CurrentURI>"
        f"<CurrentURIMetaData>{metadata}</CurrentURIMetaData>"
    )
    # Reset transport state first — LG webOS returns 701 "Transition not
    # available" on SetAVTransportURI if the renderer is still in PLAYING or
    # TRANSITIONING state from a previous session.
    try:
        _soap_action(device.control_url, "Stop")
    except Exception:
        pass  # OK if already stopped or no previous media

    _soap_action(device.control_url, "SetAVTransportURI", args)

    # Poll transport state until the TV is ready, then send Play.
    # LG webOS TVs probe the stream after SetAVTransportURI and need time
    # to transition from TRANSITIONING/NO_MEDIA_PRESENT to STOPPED.
    for attempt in range(8):
        time.sleep(2)
        state = _get_transport_state(device.control_url)
        log.debug("Transport state: %s (attempt %d/8)", state, attempt + 1)

        if state == "PLAYING":
            log.info("TV auto-started playback")
            return

        try:
            _soap_action(device.control_url, "Play", "<Speed>1</Speed>")
            break
        except Exception:
            if attempt == 7:
                raise
            log.debug("Play not ready, retrying (%d/8)...", attempt + 1)

    log.info("Now casting to %s", device.name)


def stop(device: Device) -> None:
    """Send Stop SOAP action to a DLNA renderer."""
    _soap_action(device.control_url, "Stop")
