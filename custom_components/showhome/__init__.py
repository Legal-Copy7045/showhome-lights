"""Holiday ShowHome Bluetooth Christmas lights."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.components import persistent_notification
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_PIN, CONF_STRANDS, DOMAIN, SIGNAL_STRANDS_UPDATED
from .mesh import MeshError, ShowHomeMesh

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.LIGHT]

type ShowHomeConfigEntry = ConfigEntry[ShowHomeMesh]

SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required("p1"): vol.All(vol.Coerce(int), vol.Range(0, 255)),
        vol.Required("p2"): vol.All(vol.Coerce(int), vol.Range(0, 255)),
        vol.Required("p3"): vol.All(vol.Coerce(int), vol.Range(0, 255)),
        vol.Optional("mesh_id", default=0): vol.All(vol.Coerce(int), vol.Range(0, 0xFFFF)),
        vol.Optional("via"): cv.string,
    }
)
DISCOVER_SCHEMA = vol.Schema({vol.Optional("via"): cv.string})
INJECT_SCHEMA = vol.Schema(
    {
        vol.Required("packet"): cv.string,
        vol.Optional("via"): cv.string,
        vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(1, 50)),
    }
)
CAPTURE_SCHEMA = vol.Schema(
    {
        vol.Optional("seconds", default=45): vol.All(vol.Coerce(int), vol.Range(5, 180)),
        vol.Optional("all", default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ShowHomeConfigEntry) -> bool:
    # The password can be set at install (data) or changed later (options).
    mesh = ShowHomeMesh(hass, entry.options.get(CONF_PIN, entry.data.get(CONF_PIN, "")))
    mesh.options_snapshot = dict(entry.options)
    entry.runtime_data = mesh

    @callback
    def _identity(mesh_id: int, name: str) -> None:
        strands = dict(entry.data.get(CONF_STRANDS, {}))
        if strands.get(name) == mesh_id:
            return
        _LOGGER.info("Found ShowHome light %s (mesh id %04x)", name, mesh_id)
        strands[name] = mesh_id
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_STRANDS: strands})
        async_dispatcher_send(hass, SIGNAL_STRANDS_UPDATED)

    mesh.set_identity_callback(lambda i, n: hass.loop.call_soon_threadsafe(_identity, i, n))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async def _discover(call: ServiceCall) -> None:
        try:
            if "via" in call.data:
                await mesh.async_use_via(call.data["via"])
            await mesh.async_discover()
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err

    async def _send_raw(call: ServiceCall) -> None:
        try:
            if "via" in call.data:
                await mesh.async_use_via(call.data["via"])
            await mesh.async_send_setting(call.data["mesh_id"], call.data["p1"], call.data["p2"], call.data["p3"])
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err

    async def _find_key(call: ServiceCall) -> ServiceResponse:
        try:
            result = await mesh.async_find_key(call.data.get("via"))
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err
        found = result.get("found")
        persistent_notification.async_create(
            hass,
            (f"Working key formula: **{found}**" if found else "No standard formula matched — see the service response.")
            + "\n\nResults:\n"
            + "\n".join(f"- {r['formula']}: {r['result']}" for r in result["results"]),
            title="ShowHome key finder",
            notification_id="showhome_find_key",
        )
        return result

    async def _read_chars(call: ServiceCall) -> ServiceResponse:
        try:
            return await mesh.async_read_chars(call.data.get("via"))
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err

    async def _inject(call: ServiceCall) -> None:
        try:
            await mesh.async_inject(call.data["packet"], call.data.get("via"), call.data.get("repeat", 1))
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err

    async def _capture_adverts(call: ServiceCall) -> ServiceResponse:
        summary = await mesh.async_capture_adverts(
            int(call.data.get("seconds", 45)), bool(call.data.get("all", False))
        )
        persistent_notification.async_create(
            hass,
            f"Captured {summary['total_adverts']} adverts "
            f"({summary['la_adverts']} from lights, {summary['adverts_with_mfr_or_svc_data']} "
            f"carrying extra data) from {summary['distinct_addresses']} devices.",
            title="ShowHome advert capture",
            notification_id="showhome_capture",
        )
        return summary

    hass.services.async_register(DOMAIN, "discover", _discover, schema=DISCOVER_SCHEMA)
    hass.services.async_register(DOMAIN, "send_raw", _send_raw, schema=SEND_RAW_SCHEMA)
    hass.services.async_register(
        DOMAIN, "find_key", _find_key, schema=DISCOVER_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "read_chars", _read_chars, schema=DISCOVER_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "capture_adverts", _capture_adverts, schema=CAPTURE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "inject", _inject, schema=INJECT_SCHEMA)

    if not entry.data.get(CONF_STRANDS):
        entry.async_create_background_task(hass, _first_discovery(mesh), "showhome_discovery")
    return True


async def _first_discovery(mesh: ShowHomeMesh) -> None:
    try:
        await mesh.async_discover()
    except MeshError as err:
        _LOGGER.warning("ShowHome discovery failed, run showhome.discover later: %s", err)


async def _async_options_updated(hass: HomeAssistant, entry: ShowHomeConfigEntry) -> None:
    # Discovered strands are saved into entry.data, which also fires this
    # listener; only reload when the user actually changed the options.
    if dict(entry.options) != entry.runtime_data.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ShowHomeConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_disconnect()
        hass.services.async_remove(DOMAIN, "discover")
        hass.services.async_remove(DOMAIN, "send_raw")
        hass.services.async_remove(DOMAIN, "find_key")
        hass.services.async_remove(DOMAIN, "read_chars")
        hass.services.async_remove(DOMAIN, "capture_adverts")
        hass.services.async_remove(DOMAIN, "inject")
    return ok
