"""Light entities: one for the whole mesh plus one per discovered strand.

The lights never report their state back, so every entity uses assumed state and
restores its last known state after a restart.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ShowHomeConfigEntry
from .const import (
    COLOR_SLOTS,
    CONF_BRIGHTNESS_LEVELS,
    CONF_STRANDS,
    DEFAULT_BRIGHTNESS_LEVELS,
    DOMAIN,
    EFFECT_SLOTS,
    KNOWN_COLORS,
    SIGNAL_BROADCAST_STATE,
    SIGNAL_STRANDS_UPDATED,
)
from .csrmesh import BROADCAST, MODE_COLOR, MODE_EFFECT, product_for, short_code
from .mesh import MeshError, ShowHomeMesh


def _effect_names() -> list[str]:
    colors = [f"{KNOWN_COLORS[i]} (colour {i})" if i in KNOWN_COLORS else f"Colour {i}" for i in range(COLOR_SLOTS)]
    return colors + [f"Effect {i}" for i in range(EFFECT_SLOTS)]


EFFECTS = _effect_names()


def _effect_to_mode(effect: str) -> tuple[int, int]:
    idx = EFFECTS.index(effect)
    return (MODE_COLOR, idx) if idx < COLOR_SLOTS else (MODE_EFFECT, idx - COLOR_SLOTS)


async def async_setup_entry(
    hass: HomeAssistant, entry: ShowHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mesh = entry.runtime_data
    levels = entry.options.get(CONF_BRIGHTNESS_LEVELS, DEFAULT_BRIGHTNESS_LEVELS)
    known: set[str] = set()

    async_add_entities([ShowHomeLight(mesh, entry.entry_id, BROADCAST, None, levels)])

    @callback
    def _add_new() -> None:
        new = []
        for name, mesh_id in entry.data.get(CONF_STRANDS, {}).items():
            if name not in known:
                known.add(name)
                new.append(ShowHomeLight(mesh, entry.entry_id, mesh_id, name, levels))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_STRANDS_UPDATED, _add_new))


class ShowHomeLight(LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = EFFECTS

    def __init__(self, mesh: ShowHomeMesh, entry_id: str, mesh_id: int, name: str | None, levels: int) -> None:
        self._mesh = mesh
        self._mesh_id = mesh_id
        self._levels = max(1, min(255, levels))
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_effect = EFFECTS[1]  # Red
        if name is None:
            self._attr_unique_id = f"{entry_id}_all"
            self._attr_name = None
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry_id)}, name="Christmas Lights (all)", manufacturer="Holiday ShowHome"
            )
        else:
            self._attr_unique_id = name
            self._attr_name = None
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, name)},
                name=f"{product_for(name)} {short_code(name)}",
                manufacturer="Holiday ShowHome",
                model=product_for(name),
                serial_number=name,
                via_device=(DOMAIN, entry_id),
            )
        self._attr_extra_state_attributes = {"mesh_id": mesh_id}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
            if last.attributes.get(ATTR_BRIGHTNESS):
                self._attr_brightness = last.attributes[ATTR_BRIGHTNESS]
            if last.attributes.get(ATTR_EFFECT) in EFFECTS:
                self._attr_effect = last.attributes[ATTR_EFFECT]
        if self._mesh_id != BROADCAST:
            self.async_on_remove(
                async_dispatcher_connect(self.hass, SIGNAL_BROADCAST_STATE, self._on_broadcast)
            )

    @callback
    def _on_broadcast(self, is_on: bool, brightness: int, effect: str) -> None:
        self._attr_is_on, self._attr_brightness, self._attr_effect = is_on, brightness, effect
        self.async_write_ha_state()

    def _level(self, brightness: int) -> int:
        return max(1, round(brightness * self._levels / 255))

    async def _apply(self, is_on: bool, brightness: int, effect: str) -> None:
        mode, value = _effect_to_mode(effect)
        try:
            await self._mesh.async_send_setting(self._mesh_id, mode, value, self._level(brightness) if is_on else 0)
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_is_on, self._attr_brightness, self._attr_effect = is_on, brightness, effect
        self.async_write_ha_state()
        if self._mesh_id == BROADCAST:
            async_dispatcher_send(self.hass, SIGNAL_BROADCAST_STATE, is_on, brightness, effect)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply(
            True,
            kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255),
            kwargs.get(ATTR_EFFECT, self._attr_effect or EFFECTS[1]),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply(False, self._attr_brightness or 255, self._attr_effect or EFFECTS[1])
