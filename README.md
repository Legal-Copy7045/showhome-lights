# Holiday ShowHome Lights for Home Assistant

Control Bluetooth Christmas lights made for the **Holiday ShowHome / Show App**
(icicles, snowflakes, LED balls, trees, rope lights) from Home Assistant, through
any Bluetooth adapter or ESPHome Bluetooth proxy.

The lights advertise as `LA…` names with service `0xFEF1` and talk encrypted
CSRmesh. All lights form one mesh, so Home Assistant connects to the strongest
one and reaches every strand through it.

## Install

1. HACS → Custom repositories → add this repo as an *Integration*, install, restart.
2. Close the Show App on your phone (a light accepts one Bluetooth connection).
3. Home Assistant discovers the lights; open the notification and click **Configure**.
   Leave the password blank unless you set one in the Show App (key icon).

## What you get

| Entity | Controls |
|---|---|
| `light.christmas_lights_all` | Every light at once (mesh broadcast) |
| One light per strand, e.g. `light.icicles_f9ec` | That strand only |

Strands are found automatically on first setup (run `showhome.discover` to find new ones).
Each light has on/off, brightness and an **effect** list: `Colour N` picks a
fixed colour, `Effect N` picks an animation.

The lights never report their state, so states are assumed (last command sent).
Home Assistant drops the Bluetooth link after 60 s idle so the Show App can connect again.

## Not yet known

Only colours 0 (blue), 1 (red) and 2 (green) are named; the rest are numbered.
The real number of brightness steps is not known: set it in the integration's
options (default 10). Use `showhome.send_raw` to experiment:

```yaml
action: showhome.send_raw
data: { p1: 1, p2: 5, p3: 3, mesh_id: 0 }   # mode 1=colour/2=effect, value, brightness (0=off)
```

## Protocol credit

Packet format and crypto from [craus81/Christmas-lights](https://github.com/craus81/Christmas-lights),
itself based on [nkaminski/csrmesh](https://github.com/nkaminski/csrmesh).

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```
