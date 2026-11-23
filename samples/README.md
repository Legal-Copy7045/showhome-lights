# Sample captures

Real advertisement captures from the lights, for anyone trying to crack the key
derivation. Captured passively via Home Assistant ESPHome Bluetooth proxies.

- **`red_all_lights_123456.txt`** — hundreds of `0xFEF1` service-data payloads
  recorded while the app sent **only "Red" (all lights)**, repeatedly, with the
  app password set to `123456` (a throwaway). Because every packet is the *same*
  command, the correct key must decrypt them to a near-constant plaintext — see
  [`../docs/FINDINGS.md`](../docs/FINDINGS.md). Lines with `src=0080` (bytes 3–4)
  are app-originated commands; others are lights relaying.

If you work out the key derivation from these, please open an issue or PR — see
the FINDINGS doc for exactly what's known and what's been ruled out.
