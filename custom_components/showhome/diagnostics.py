"""Diagnostics: BLE traffic history for debugging the light protocol."""

from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import ShowHomeConfigEntry
from .const import CONF_STRANDS
from .mesh import is_showhome


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ShowHomeConfigEntry) -> dict[str, Any]:
    mesh = entry.runtime_data
    return {
        "pin_set": bool(entry.options.get("pin", entry.data.get("pin"))),
        "options": dict(entry.options),
        "strands": entry.data.get(CONF_STRANDS, {}),
        "connected_via": mesh.connected_name,
        "gatt_services": mesh.gatt_services,
        "lights_in_range": [
            {"name": i.name, "address": i.address, "rssi": i.rssi, "source": i.source, "connectable": i.connectable}
            for i in bluetooth.async_discovered_service_info(hass, connectable=False)
            if is_showhome(i)
        ],
        "history": list(mesh.history),
    }
