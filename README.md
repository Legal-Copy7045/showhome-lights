# Holiday ShowHome Lights for Home Assistant

A Home Assistant integration for the Bluetooth Christmas lights sold for the
**Holiday ShowHome / "Show App"** — LED balls, snowflakes, icicles, trees and
rope lights. They advertise as `LA…` names with service UUID `0xFEF1` and speak
**CSRmesh**.

> ## ⚠️ Status: research / not yet controlling — help wanted
>
> **This integration reliably *sees* the lights and can *capture* their command
> traffic, but it cannot yet *control* them.** The commands are encrypted, and
> the key these particular products derive from the app password is **not** the
> standard CSRmesh formula — so we can't forge or decrypt commands yet. We've
> taken it as far as the lights themselves allow without the app's internals,
> and we're **stopping here for now**.
>
> **If you know this hardware, CSRmesh, or can read the Show App's key
> derivation — we'd love your feedback, ideas, or a PR.** See
> [What's blocking us](#whats-blocking-us) and
> [docs/FINDINGS.md](docs/FINDINGS.md), and there's a real captured dataset to
> work from in [`samples/`](samples/). Please open an issue.

## What works today

- **Discovery** — auto-detects the lights over any Bluetooth adapter or ESPHome
  Bluetooth proxy, and creates a light entity per strand plus an "all lights" one.
- **Reliable connection** — connects through the strongest light, recovers from
  the empty-service-cache problem on reconnect, and drops the link after 60 s idle.
- **Over-the-air capture** — the lights rebroadcast every command across the mesh
  as advertisements, and the proxies hear them. The `showhome.capture_adverts`
  action records them all (see below). **This is the key discovery of the project.**
- **A full, tested integration skeleton** — the moment the key derivation is
  known, sending commands is a tiny change. 19+ passing tests.

## What does NOT work yet

- **Turning the lights on/off or changing colour from Home Assistant.** Commands
  are sent in the correct shape but the lights reject them, because the
  encryption key is wrong (see below). The light entities therefore don't
  actually control anything yet.

## What's blocking us

Two walls, both living inside the Show App, not in Home Assistant:

1. **Unknown key derivation.** The reference project
   ([craus81/Christmas-lights](https://github.com/craus81/Christmas-lights))
   showed this app normally derives its CSRmesh key as
   `reversed(SHA256(password + b"\x00MCP"))[:16]`. We captured hundreds of real
   single-command ("Red") packets with a known password (`123456`) and tested
   that formula plus eight others across every AES mode and framing. **All
   produced noise** — so these products transform the password differently
   before deriving the key (or "Red" is a varying per-frame effect, not one
   fixed command). Cracking this needs the app's actual key-derivation code.
2. **Replay is blocked.** The lights track a per-source sequence number and
   reject any command older than the last one seen, so we can't simply replay a
   captured packet. Forging a fresh one needs the key from wall #1.

Full technical detail, including the packet layout and everything tried, is in
**[docs/FINDINGS.md](docs/FINDINGS.md)**. A real capture to experiment with is in
**[`samples/red_all_lights_123456.txt`](samples/red_all_lights_123456.txt)**.

## Install (for tinkering / to help)

1. HACS → Custom repositories → add this repo as an *Integration*, install, restart.
2. Settings → Devices & Services → it should discover the lights. In **Configure**,
   enter the app password (or leave blank).

## Diagnostic / research actions

| Action | What it does |
|---|---|
| `showhome.discover` | Ask the mesh to announce its lights (they don't always answer). |
| `showhome.capture_adverts` | Record every advertisement the proxies hear for N seconds (`seconds`, `all`). Read it back from the integration's **Download diagnostics**. This is how command traffic is captured. |
| `showhome.send_raw` | Send a raw `p1,p2,p3` command (needs the right key to work). |
| `showhome.inject` | Write a raw captured packet to the control characteristic (replay; currently blocked by sequence numbers). |
| `showhome.find_key` | Try standard key formulas and see which the lights answer to. |
| `showhome.read_chars` | Dump the readable `0xFEF1` characteristics. |

### How to capture command traffic (to help crack it)

1. Set a known password in the Show App and enter the same in the integration's **Configure**.
2. Call `showhome.capture_adverts` with `seconds: 60, all: true`.
3. While it runs, drive the lights from the Show App — ideally **one command
   repeatedly** (e.g. only Red), so every captured packet is the same command.
4. Open the integration → **Download diagnostics**; the `capture` array holds the
   adverts. The command packets are the `svc` entries under UUID `0000fef1…`.

## How it's meant to work (once unblocked)

All lights form one CSRmesh network, so HA connects to the strongest light and
reaches the rest through it. Each product type is recognised by its name prefix
(`LA3`=Icicles, `LA4`=Snowflake, `LAB`=LED Ball, `LAC`=Tree, `LA7`=Rope). The
integration already builds per-strand and "all lights" entities with on/off,
brightness and an effect list; they just need a key that the lights accept.

## Credits

- Protocol starting point: [craus81/Christmas-lights](https://github.com/craus81/Christmas-lights)
- CSRmesh crypto reference: [nkaminski/csrmesh](https://github.com/nkaminski/csrmesh)

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT
