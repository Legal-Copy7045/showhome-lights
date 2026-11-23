"""Config flow: discovered automatically from any light's Bluetooth advert."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CONF_BRIGHTNESS_LEVELS, CONF_PIN, CONF_STRANDS, DEFAULT_BRIGHTNESS_LEVELS, DOMAIN
from .csrmesh import product_for, short_code
from .mesh import is_showhome

# One entry covers the whole mesh, whichever light was seen first.
MESH_UNIQUE_ID = "showhome_mesh"


class ShowHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._seen: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        if not is_showhome(discovery_info):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(MESH_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        name = discovery_info.name or discovery_info.address
        self._seen = name
        self.context["title_placeholders"] = {"name": f"{product_for(name)} {short_code(name)}"}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(MESH_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(
                title="Christmas Lights",
                data={CONF_PIN: user_input.get(CONF_PIN, ""), CONF_STRANDS: {}},
            )
        lights = sorted({i.name for i in async_discovered_service_info(self.hass) if is_showhome(i)})
        if not lights:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_PIN, default=""): str}),
            description_placeholders={
                "lights": ", ".join(f"{product_for(n)} {short_code(n)}" for n in lights),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ShowHomeOptionsFlow()


class ShowHomeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_BRIGHTNESS_LEVELS, DEFAULT_BRIGHTNESS_LEVELS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_BRIGHTNESS_LEVELS, default=current): vol.All(vol.Coerce(int), vol.Range(1, 255))}
            ),
        )
