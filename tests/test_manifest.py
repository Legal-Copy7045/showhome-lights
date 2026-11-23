"""The bluetooth matchers must pass HA's own validation, or HA's bluetooth
integration (and everything depending on it) fails to start."""

import json
from pathlib import Path

from homeassistant.components.bluetooth.match import BluetoothMatcherIndex

MANIFEST = Path(__file__).parents[1] / "custom_components/showhome/manifest.json"


def test_bluetooth_matchers_are_accepted_by_home_assistant():
    matchers = json.loads(MANIFEST.read_text())["bluetooth"]
    index = BluetoothMatcherIndex()
    for matcher in matchers:
        index.add({**matcher, "domain": "showhome"})
    index.build()
    for m in matchers:
        assert len(m["local_name"].split("*")[0]) >= 3
