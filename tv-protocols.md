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

#### Querying device capabilities
Every DLNA renderer has a **ConnectionManager** service alongside AVTransport. Its `GetProtocolInfo` SOAP action returns a comma-separated `Sink` list of every format the device accepts:

```
http-get:*:<mime>:<dlna_profile_or_wildcard>
```

Example entries from an LG TV's sink list:
```
http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_BL_CIF15_AAC_520
http-get:*:video/mpeg:DLNA.ORG_PN=AVC_TS_NA_ISO
http-get:*:video/mp4:*          ← wildcard (accepts any video/mp4)
http-get:*:video/mpeg:*         ← wildcard (accepts any video/mpeg)
http-get:*:video/mp2t:*
```

This is useful for debugging format issues — if the TV rejects content, check whether its sink list includes the MIME type and profile you're sending.

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
- **In qast**: Handled entirely by `pychromecast` (which depends on the `zeroconf` Python library). We don't implement mDNS ourselves — it's complex and the library handles it well
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

#### Stream format: MPEG-TS

DLNA devices are served raw MPEG-TS (no master muxer / fMP4 remux). This was chosen over fragmented MP4 because:

- **Universal compatibility**: Every DLNA renderer tested accepts MPEG-TS. LG webOS TVs reject fragmented MP4 (Play returns UPnP error 501 "Action Failed") but handle MPEG-TS fine.
- **Joinable at any point**: MPEG-TS packets are self-contained. If the TV probes the stream during SetAVTransportURI and consumes data from the ring buffer, subsequent connections can start from any packet boundary. fMP4 requires an init segment (ftyp+moov) at the start of every connection — if the init is consumed by a probe, reconnections fail.
- **Simpler pipeline**: Skips the master muxer process entirely. The TS rewriter writes directly to the ring buffer.
- **No A/V sync correction needed**: DLNA renderers use PTS for both audio and video timing, so there's no clock drift across segment boundaries (unlike Chromecast/fMP4 which needs video PTS correction).

The HTTP Content-Type is `video/mpeg` and the DIDL-Lite protocolInfo matches: `http-get:*:video/mpeg:<dlna_flags>`.

#### Sending media
1. **Stop** (best-effort) — reset transport state. LG webOS returns 701 "Transition not available" on SetAVTransportURI if the renderer is still PLAYING from a previous session.
2. **SetAVTransportURI** — tell the TV what URL to play, with DIDL-Lite metadata. The TV will immediately start probing the stream URL (sending GET requests) — this is normal.
3. **Poll GetTransportInfo + Play** — poll the TV's transport state every 2 seconds and attempt Play. Up to 8 attempts (16 seconds total). Some TVs (LG webOS) need time to transition through TRANSITIONING state after the probe. Some TVs auto-start playback without needing an explicit Play command — detect this by checking for `PLAYING` state.

#### SOAP request format
```
SOAPAction: "urn:schemas-upnp-org:service:AVTransport:1#ActionName"
Content-Type: text/xml; charset="utf-8"
Body: XML envelope with <InstanceID>0</InstanceID> + action-specific args
```

#### DIDL-Lite metadata
Embedded in SetAVTransportURI, HTML-escaped. Contains:
- `<dc:title>` — display name
- `<res protocolInfo="http-get:*:video/mpeg:<dlna_flags>">URL</res>` — the media URL, MIME type, and DLNA capability flags
- `<upnp:class>object.item.videoItem.videoBroadcast</upnp:class>` — broadcast class (tells TV this is live/streaming content, not a seekable file)

The DLNA flags in the protocolInfo fourth field should match the flags sent in the HTTP `contentFeatures.dlna.org` header.

#### DLNA flags breakdown

The flags string `DLNA.ORG_OP=00;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000` means:

- **OP=00**: No seek support (neither time-based nor byte-based). Correct for a forward-only stream.
- **CI=1**: Content is transcoded. Tells the renderer the stream has been converted from its original format (which it has — qast always transcodes to H.264/AAC).
- **FLAGS=01700000...**: Bitmask (128-bit, first 32 bits matter):

| Bit | Hex | Flag | Set? |
|-----|-----|------|------|
| 24 | 0x01000000 | streaming_transfer_mode | Yes |
| 22 | 0x00400000 | background_transfer_mode | Yes |
| 21 | 0x00200000 | connection_stalling | Yes |
| 20 | 0x00100000 | DLNA v1.5 | Yes |

The s0/sN_increasing flags (bits 27-26, the DLNA "live content" indicators) are not set — in practice they caused some renderers to behave unpredictably. The `videoBroadcast` upnp:class in the DIDL-Lite metadata serves the same purpose of signaling live/streaming content.

#### HTTP server requirements
DLNA renderers expect specific HTTP headers on the stream response:
- `Content-Type: video/mpeg`
- `Accept-Ranges: none` — explicitly tells the TV not to attempt range/seek requests
- `contentFeatures.dlna.org: <DLNA_FLAGS>` — same flags string as in the DIDL-Lite protocolInfo
- `transferMode.dlna.org: Streaming`
- `Connection: close` — signals end-of-data (no Content-Length for a continuous stream)

#### Probe disconnect handling
After SetAVTransportURI, TVs send one or more GET requests to probe the stream before accepting Play. These probe connections download data from the ring buffer and then disconnect. This is normal behavior — **not** a real client disconnect. The disconnect_event must be cleared after cast_media() returns, otherwise the monitoring loop will see it and trigger spurious re-cast attempts (creating an infinite re-cast loop).

#### Monitoring playback
- Poll **GetTransportInfo** — response contains `CurrentTransportState`: `PLAYING`, `STOPPED`, `NO_MEDIA_PRESENT`, `TRANSITIONING`, `PAUSED_PLAYBACK`
- `STOPPED` or `NO_MEDIA_PRESENT` = done

#### Stopping
- Send **Stop** SOAP action

#### Known quirks
- **LG webOS**: Slow to discover (needs 15s+ timeout with repeated M-SEARCH). Rejects fragmented MP4 over DLNA (UPnP error 501) — must use MPEG-TS. Probes the stream aggressively after SetAVTransportURI. May not auto-switch input — user may need to be on Home screen or manually open Media Player app. Always shows transport overlay (progress bar) regardless of DLNA live flags or videoBroadcast class — this is a firmware UI behavior that cannot be suppressed via standard DLNA.
- **Samsung**: Generally more reliable. Accepts both fMP4 and MPEG-TS. May also probe the stream after SetAVTransportURI (same disconnect handling applies).
- **Sonos**: Shows up as a MediaRenderer but is audio-only. Will reject video MIME types with UPnP error 714 on SetAVTransportURI.

### Roku ECP (External Control Protocol)
Control is done via simple HTTP requests to the Roku's base URL (port 8060). Discovered via SSDP with `ST: roku:ecp`.

#### Three relevant Roku channels

**Play on Roku (channel 15985)** — A hidden built-in system channel that accepts arbitrary HTTP video URLs. This is what Home Assistant's Roku integration uses. Community reverse-engineered, no official docs. Accepts URLs via `/input/15985` (not `/launch`):
```
POST http://ROKU_IP:8060/input/15985?t=v&u=ENCODED_URL&videoName=qast&videoFormat=mp4
```
**Disabled in newer firmware** — Returns HTTP 404 via both `/input/15985` and `/launch/15985` on Roku OS 15.1.4 (ONN Roku TV). Verified 2025-02-24.

**Media Assistant (channel 782875)** — A free community replacement from the Roku Channel Store. Accepts the same parameters as Play on Roku but via `/launch/782875`. This is what qast uses. Must be installed manually by the user.

**Roku Media Player (channel 2213)** — A file browser that only plays from DLNA media servers or USB storage. **Cannot accept arbitrary HTTP URLs** — not useful for URL-based casting.

#### Sending media
```
POST http://ROKU_IP:8060/launch/782875?t=v&u=ENCODED_URL&videoName=qast&videoFormat=ts
```
- URL must be percent-encoded
- `t=v` = video type
- `videoFormat` = `ts` (MPEG-TS) or `mp4`
- No SOAP, no XML body — just a POST with empty body
- Returns 200 on success, 404 if the channel is not installed

#### Requirements
- **Media Assistant** app (free, channel 782875) must be installed from the Roku Channel Store
- Settings > System > Advanced System Settings > Control by mobile apps must be set to **Enabled**

#### Launching native apps
- **POST** `/launch/{channel_id}?params` — launch a channel with parameters
- YouTube channel ID: `837`. Params: `contentId=VIDEO_ID&MediaType=live`
- Native app launches give **no playback state feedback** — `/query/media-player` only tracks the system media player, not in-app players
- Can only monitor via `/query/active-app` to check if the app is still in the foreground

#### Monitoring (system media player)
- **GET** `/query/media-player` — returns XML with `state` attribute: `play`, `pause`, `buffer`, `close`
- `state="close"` = done

#### Monitoring (app launches)
- **GET** `/query/active-app` — returns XML with active app element including `id` attribute
- Compare against launched app ID to detect if user navigated away

#### Stopping
- **POST** `/keypress/Home` — sends the user to the Home screen (stops playback)

---

## Devices tested

| Device | Protocol | IP | Notes |
|--------|----------|----|-------|
| **LG webOS TV** | DLNA | 192.168.2.47:1554 | Slow to discover. Must use MPEG-TS (rejects fMP4). Probes stream after SetAVTransportURI. Always shows transport overlay — cannot be suppressed via DLNA. May need manual input switch. |
| **Samsung Frame TV (QN55LS03)** | DLNA | 192.168.2.31:9197 | Shows as "Orbit". Works with both fMP4 and MPEG-TS. Probes stream after SetAVTransportURI (clear disconnect_event after casting). |
| **ONN Roku TV** | Roku ECP | 192.168.2.51:8060 | Shows as "EE Department TV". Requires Media Assistant app installed. |
| **Sonos One** | DLNA | 192.168.2.73:1400 | Audio-only renderer. Rejects video with UPnP error 714. |

### GetProtocolInfo results (video sink formats)

**LG webOS TV** — Wildcards: `video/mp4:*`, `video/mpeg:*`, `video/mp2t:*`, `video/mp2ts:*`, `video/mts:*`, `video/x-matroska:*`. Specific AVC profiles: `AVC_MP4_BL_CIF15_AAC_520`, `AVC_TS_NA_T`, `AVC_TS_NA_ISO`.

**Samsung Frame TV** — Wildcards: `video/mp4:*`, `video/mpeg:*`, `video/mpeg2:*`, `video/webm:*`, `video/hevc:*`, `video/x-mkv:*`. Extensive specific profiles including many AVC_MP4 (SD through HD) and AVC_TS variants.

Both TVs accept `video/mpeg:*` (MPEG-TS) and `video/mp4:*` — but LG fails on fMP4 in practice despite the wildcard. The wildcard means the TV will attempt to play the format, not that it will succeed.
