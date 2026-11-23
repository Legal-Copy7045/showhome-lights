# Findings — Holiday ShowHome lights reverse-engineering

This is a working log of what we learned trying to control the Holiday ShowHome
("Show App") Bluetooth Christmas lights from Home Assistant. It's written so
someone else can pick up where we stopped. **Contributions, corrections and
PRs very welcome — please open an issue.**

## The hardware

- Products: "LED Ball", "Snowflake", "Icicles", "5.5' Tree" (and the rope light
  others have documented). Controlled by the **Holiday ShowHome / "Show App"** (iOS).
- BLE: each light advertises a name `LA<type><id>` and service UUID
  `0000fef1-0000-1000-8000-00805f9b34fb`. Name prefixes seen:
  `LA3`=Icicles, `LA4`=Snowflake, `LAB`=LED Ball, `LAC`=Tree, `LA7`=Rope.
- This is **CSRmesh** (the classic CSR/Qualcomm `0xFEF1` mesh), same family as
  [craus81/Christmas-lights](https://github.com/craus81/Christmas-lights) and
  [nkaminski/csrmesh](https://github.com/nkaminski/csrmesh).

## GATT layout (service 0xFEF1)

```
c4edc000-9daf-11e3-8000-00025b000b00  write
c4edc000-9daf-11e3-8001-00025b000b00  read
c4edc000-9daf-11e3-8002-00025b000b00  read, write
c4edc000-9daf-11e3-8003-00025b000b00  write-without-response, write, notify   <- MTL continue
c4edc000-9daf-11e3-8004-00025b000b00  write-without-response, write, notify   <- MTL complete
c4edc000-9daf-11e3-8005-00025b000b00  read, write
c4edc000-9daf-11e3-8006-00025b000b00  read, write
```

Commands are written to `8003`/`8004` (MTL bridge). The readable characteristics
(`8001/8002/8005/8006`) returned only zeros / not-permitted — no key material.

A connection reaches the whole mesh: the connected light relays to the others.
Only one BLE connection at a time, so the Show App must be closed for HA to connect.

## The important discovery: commands are rebroadcast as advertisements

When the app sends a command, the lights **rebroadcast it across the mesh as BLE
advertisements** carrying `0xFEF1` service data. Passive scanners (ESPHome
Bluetooth proxies) can hear these. So you can capture the entire command stream
with no sniffer dongle — see `showhome.capture_adverts` and
[`samples/`](../samples). This is the main positive result.

Note: the GATT connection between the phone and a light (the actual
app→light link) is **not** visible to proxies — only the mesh rebroadcast
advertisements are.

## On-air packet layout

Each `0xFEF1` service-data payload is 27 bytes. Believed layout (matches the
CSRmesh network PDU in the references):

```
 seq[3]  src[2]  ciphertext[13]  mic[8]  ttl[1]
   |       |          |            |       |
   |       |          |            |       `- rebroadcast/TTL counter; cycles fc/fd/fe
   |       |          |            |          for the same seq. OUTSIDE the mic.
   |       |          |            `- 8-byte message integrity check
   |       |          `- AES-encrypted payload (OFB in the reference scheme)
   |       `- source, little-endian. 0x8000 = controller/app; other = a light.
   `- sequence counter, little-endian, increments per command.
```

Evidence:
- `src` is `00 80` (0x8000) for app-originated commands — matches the reference's
  controller source id.
- `seq` increments monotonically as you tap commands; the middle byte rolls when
  the low byte wraps (`…c5ba` → `…c6ba`), confirming 3-byte little-endian.
- The same `seq` appears 3× with the last byte cycling `fc/fd/fe`, and the rest
  identical — so that byte is a rebroadcast counter outside the MIC.

## The reference crypto (what normally works, but not here)

From craus81 / nkaminski, for this same app's rope light:

- key = `reversed(SHA256(passphrase + b"\x00MCP"))[:16]`
- cipher = AES-128-OFB, IV = `seq[3] + b"\x00" + src[2] + b"\x00"*10`
- mic = `reversed(HMAC_SHA256(key, b"\x00"*8 + seq + src + ciphertext))[:8]`
- command plaintext (rope light):
  `dest[2]  0x73  B0  0x00  0xFF  0xFE  B0  P1  P2  P3  0x00  0x00`
  where `B0 = (counter<<4)|3`, `dest=0x0000` means all lights, and observed
  params include `01 01 xx` red, `01 02 xx` green, `01 00 xx` blue, `02 xx xx`
  effects (`xx` = brightness, `00` = off).

This is implemented in [`custom_components/showhome/csrmesh.py`](../custom_components/showhome/csrmesh.py)
and unit-tested against the reference — the implementation is correct for that scheme.

## Why it doesn't work on these lights

With a **known** password (`123456`, set on all balls) we captured hundreds of
**single-command** "Red (all lights)" packets (see
[`samples/red_all_lights_123456.txt`](../samples/red_all_lights_123456.txt)).

For the *correct* key, every same-command packet must decrypt to near-identical
plaintext (only a counter byte changing), so most byte positions would be
constant. We swept:

- **key formulas** (9): `reversed(SHA256(pw+"\x00MCP"))[:16]`, `SHA256(pw+"\x00MCP")[:16]`,
  `reversed(SHA256(pw))`, `SHA256(pw)`, `SHA256(pw)[16:]`, `MD5(pw)`, `MD5(pw+"\x00MCP")`,
  `SHA1(pw)`, `SHA1(pw+"\x00MCP")`, and `reversed(SHA256("\x00MCP"+pw))`.
- **cipher modes**: OFB, CTR, CFB, CBC, ECB.
- **IV constructions** (6 variants of seq/src placement).
- **ciphertext framing**: 13, 14, 16 bytes.

**Result: zero stable byte positions for every combination.** i.e. none of these
keys is correct. The MAC also never verified. Conclusions:

1. The app does **not** feed the user's typed password directly into the standard
   formula — it transforms it somehow before deriving the network key. The
   transform lives in the app's code.
2. (Alternative possibility) "Red" on these balls may be a varying per-frame
   effect rather than one fixed command, which would defeat the
   same-command-stability method regardless of key.

We also confirmed a separate wall: **sequence-number replay protection.**
Injecting a captured, validly-signed "Red" packet (`showhome.inject`) while the
lights were blue did nothing — the stale sequence number was rejected. Forging a
fresh packet needs the key.

## What would unblock this

Any one of:

- **The app's key derivation** — decompile the iOS (or any) Show App and read how
  the password becomes the CSRmesh network/passphrase/key. This is the direct fix.
- **A capture of password provisioning** — capture the app *setting* the password
  on a light (the moment it writes the network key). This likely happens over the
  GATT connection (invisible to proxies), so it needs a connection sniffer or the
  app internals.
- **Confirmation of the command model** — is a colour one fixed packet, or a
  stream? A capture of a *static* state (e.g. steady "on, white, mid-brightness")
  held for a while, versus an animated effect, would tell us whether the
  same-command-stability attack can work at all.

## Reproducing / continuing

1. Install the integration, set a known password in the app and in **Configure**.
2. `showhome.capture_adverts` (`seconds: 60, all: true`) while tapping **one**
   command repeatedly in the app.
3. Integration → **Download diagnostics**; the `capture[].svc["0000fef1…"]` values
   are the packets. Those with `src=0x8000` (bytes 3–4 = `00 80`) are app commands.
4. Try to find a key derivation of your password that makes same-command packets
   decrypt to a stable plaintext. If you find it, `csrmesh.derive_key` and
   `csrmesh.key_candidates` are where to wire it in.

Everything here is from poking at the lights from the outside — treat the exact
field boundaries as "best current understanding", not gospel.
