# TV Discovery & Casting Protocols

## Discovery

### SSDP (Simple Service Discovery Protocol)
Both DLNA and Roku use SSDP for discovery. It works over UDP multicast:

- **Address**: `239.255.255.250:1900` (well-known UPnP multicast group)
- **Method**: Send an `M-SEARCH` HTTP-over-UDP message, devices respond with their location URL
- **Unreliable**: UDP packets can be dropped. Send M-SEARCH multiple times (3x upfront + re-send on socket timeouts) for reliability
- **MX header**: Tells devices the max seconds they can wait before responding (cap at 5 per UPnP spec). Devices randomize their response delay within this window to avoid flooding
- **Slow responders**: LG webOS TVs in particular are slow/intermittent. A 15s overall timeout with repeated sends helps

#### DLNA Discovery
- **Search target**: `ST: urn:schemas-upnp-org:device:MediaRenderer:1`
- Response includes a `LOCATION:` header pointing to a UPnP XML description
- Parse the XML for `friendlyName`, `modelName`, and crucially the **AVTransport service controlURL**
- Namespace: `urn:schemas-upnp-org:device-1-0`
- Only devices with an AVTransport service are useful for casting

#### Roku Discovery
- **Search target**: `ST: roku:ecp`
- Response `LOCATION:` points to the Roku's base URL (e.g., `http://192.168.2.73:8060/`)
- Fetch `/query/device-info` from that base URL to get `friendly-device-name`, `model-name`, etc.
- XML has no namespace (unlike DLNA)

### mDNS/DNS-SD (Chromecast / Google TV)
Chromecast and Google TV devices use a completely different discovery mechanism from SSDP:

- **Protocol**: mDNS (multicast DNS), aka Bonjour/Zeroconf
- **Address**: `224.0.0.251:5353` (mDNS multicast group, different from SSDP)
- **Service name**: `_googlecast._tcp.local`
- **How it works**: Devices register a DNS-SD service record. Clients browse for the service type and get back TXT records with device metadata (friendly name, model, device ID, etc.) plus the host/port to connect to
- **Cast protocol**: Once discovered, communication uses a protobuf-based channel over TLS — not HTTP/SOAP like DLNA or REST like Roku
- **In casturl**: Handled entirely by `pychromecast` (which depends on the `zeroconf` Python library). We don't implement mDNS ourselves — it's complex and the library handles it well
- **Optional**: If pychromecast is not installed, Cast discovery is silently skipped and DLNA/Roku still work

### Summary table

| Protocol | Discovery | Discovery address | Casting transport |
|----------|-----------|-------------------|-------------------|
| DLNA | SSDP (M-SEARCH) | `239.255.255.250:1900` | SOAP/HTTP |
| Roku | SSDP (M-SEARCH) | `239.255.255.250:1900` | REST/HTTP |
| Chromecast/Google TV | mDNS/DNS-SD | `224.0.0.251:5353` | Protobuf over TLS |

DLNA and Roku share the same SSDP multicast group but use different search targets. Chromecast is on a completely separate discovery channel.

---

## Casting

### DLNA (UPnP AVTransport SOAP)
Control is done via SOAP XML POST requests to the AVTransport controlURL discovered above.

#### Sending media
1. **SetAVTransportURI** — tell the TV what URL to play, with DIDL-Lite metadata (title, content type, resource URL)
2. **Wait 1-3 seconds** — some TVs (LG webOS) return HTTP 500 if you send Play immediately
3. **Play** — with `<Speed>1</Speed>` argument

#### SOAP request format
```
SOAPAction: "urn:schemas-upnp-org:service:AVTransport:1#ActionName"
Content-Type: text/xml; charset="utf-8"
Body: XML envelope with <InstanceID>0</InstanceID> + action-specific args
```

#### DIDL-Lite metadata
Embedded in SetAVTransportURI, HTML-escaped. Contains:
- `<dc:title>` — display name
- `<res protocolInfo="http-get:*:video/mp4:*">URL</res>` — the media URL and content type
- `<upnp:class>object.item.videoItem</upnp:class>`

#### Monitoring playback
- Poll **GetTransportInfo** every 3 seconds
- Response contains `CurrentTransportState`: `PLAYING`, `STOPPED`, `NO_MEDIA_PRESENT`, `TRANSITIONING`, `PAUSED_PLAYBACK`
- `STOPPED` or `NO_MEDIA_PRESENT` = done

#### Stopping
- Send **Stop** SOAP action

#### HTTP server requirements
DLNA renderers (especially LG, Samsung) expect specific HTTP headers:
- `contentFeatures.dlna.org: DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000`
- `transferMode.dlna.org: Streaming`
- `Accept-Ranges: bytes` with proper 206 Partial Content support
- Without these, some TVs will fetch the file but silently not display it

#### Known quirks
- **LG webOS**: Needs delay between SetAVTransportURI and Play (HTTP 500 otherwise). May not auto-switch input to show DLNA content — user may need to be on Home screen or manually open Media Player app
- **Samsung**: Generally more reliable with auto-switching but still needs DLNA headers
- **Sonos**: Shows up as a MediaRenderer but is audio-only

### Roku ECP (External Control Protocol)
Control is done via simple HTTP requests to the Roku's base URL (port 8060).

#### Launching apps
- **POST** `/launch/{channel_id}?params` — launch a channel with parameters
- YouTube channel ID: `837`. Params: `contentId=VIDEO_ID&MediaType=live`
- No SOAP, no XML body — just a POST with empty body

#### Limitations
- Launching a native app (e.g., YouTube) gives **no playback state feedback**. `/query/media-player` only tracks the Roku system media player, not in-app players
- Can only monitor via `/query/active-app` to check if the app is still in the foreground
- For playback state tracking (needed for queues), must serve media yourself and use the Roku Media Player channel instead of native apps

#### Monitoring (system media player)
- **GET** `/query/media-player` — returns XML with `state` attribute: `play`, `pause`, `buffer`, `close`
- `state="close"` = done

#### Monitoring (app launches)
- **GET** `/query/active-app` — returns XML with active app element including `id` attribute
- Compare against launched app ID to detect if user navigated away

#### Stopping
- **POST** `/keypress/Stop` — simulates remote Stop button

---

## Devices tested
- **LG webOS TV**: DLNA. Slow to discover, needs Play delay, may not auto-switch input
- **Samsung Frame TV (QN55LS03)**: DLNA. Model shows as "Orbit"
- **Roku (EE Department TV)**: Roku ECP. YouTube app launch works, media player monitoring works for served content
- **Sonos One**: DLNA MediaRenderer but audio-only
