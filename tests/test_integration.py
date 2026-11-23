"""Integration tests: config flow, entities and the bytes written over BLE."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.showhome.const import CONF_PIN, CONF_STRANDS, DOMAIN
from custom_components.showhome.csrmesh import (
    CHAR_COMPLETE,
    CHAR_CONTINUE,
    SERVICE_UUID,
    derive_key,
    make_packet,
    open_packet,
)

pytestmark = pytest.mark.usefixtures("skip_bt")


def _info(name="LA3000014F9EC", address="C5:26:04:47:87:AA", rssi=-68):
    info = MagicMock()
    info.name = name
    info.address = address
    info.rssi = rssi
    info.service_uuids = [SERVICE_UUID]
    info.device = MagicMock(address=address, name=name)
    return info


@pytest.fixture
def skip_bt(hass: HomeAssistant):
    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    with patch(
        "custom_components.showhome.mesh.bluetooth.async_discovered_service_info",
        return_value=[_info(), _info("LAB00001A92A5", "D4:47:2C:38:82:FF", -80)],
    ), patch(
        "custom_components.showhome.config_flow.async_discovered_service_info",
        return_value=[_info(), _info("LAB00001A92A5", "D4:47:2C:38:82:FF", -80)],
    ):
        yield


@pytest.fixture
def client():
    c = MagicMock()
    c.is_connected = True
    c.write_gatt_char = AsyncMock()
    c.start_notify = AsyncMock()
    c.disconnect = AsyncMock()
    with patch("custom_components.showhome.mesh.establish_connection", AsyncMock(return_value=c)) as ec:
        c.establish = ec
        yield c


def _sent(client, pin=""):
    """Decrypt every packet written to the GATT characteristics."""
    out, buf = [], b""
    for call in client.write_gatt_char.call_args_list:
        char, chunk = call.args[0], call.args[1]
        buf += chunk
        if char == CHAR_COMPLETE:
            msg = open_packet(derive_key(pin), buf)
            assert msg is not None and msg.source == 0x8000
            out.append(msg.payload)
            buf = b""
        else:
            assert char == CHAR_CONTINUE
    return out


async def test_bluetooth_discovery_creates_entry(hass: HomeAssistant, client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info())
    assert result["type"] is FlowResultType.FORM
    assert "Icicles F9EC" in result["description_placeholders"]["lights"]
    assert "LED Ball 92A5" in result["description_placeholders"]["lights"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: ""})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # a second light's advert must not create a second entry
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info("LAB00001A92A5", "D4:47:2C:38:82:FF")
    )
    assert result["type"] is FlowResultType.ABORT


async def _setup(hass, strands=None, pin=""):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PIN: pin, CONF_STRANDS: {"LA3000014F9EC": 0x0005} if strands is None else strands})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_turn_on_all_sends_broadcast_colour(hass: HomeAssistant, client) -> None:
    await _setup(hass, pin="1234")
    ent = er.async_get(hass)
    all_id = next(e.entity_id for e in ent.entities.values() if e.unique_id.endswith("_all"))
    strand_id = ent.async_get_entity_id("light", DOMAIN, "LA3000014F9EC")
    assert strand_id

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": all_id, "brightness": 255, "effect": "Green (colour 2)"}, blocking=True
    )
    payload = _sent(client, "1234")[-1]
    assert payload[0:3] == bytes([0, 0, 0x73])
    assert payload[8:11] == bytes([1, 2, 10])  # colour mode, green, top of 10 steps
    # strands follow the broadcast state
    assert hass.states.get(strand_id).state == "on"
    assert hass.states.get(strand_id).attributes["effect"] == "Green (colour 2)"

    await hass.services.async_call("light", "turn_off", {"entity_id": all_id}, blocking=True)
    assert _sent(client, "1234")[-1][8:11] == bytes([1, 2, 0])
    assert hass.states.get(strand_id).state == "off"


async def test_strand_targets_its_mesh_id(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    strand_id = er.async_get(hass).async_get_entity_id("light", DOMAIN, "LA3000014F9EC")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": strand_id, "effect": "Effect 4", "brightness": 128}, blocking=True
    )
    payload = _sent(client)[-1]
    assert payload[0:2] == bytes([5, 0])
    assert payload[8:11] == bytes([2, 4, 5])


async def test_send_raw_service(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    await hass.services.async_call(DOMAIN, "send_raw", {"p1": 1, "p2": 7, "p3": 3}, blocking=True)
    assert _sent(client)[-1][8:11] == bytes([1, 7, 3])


async def test_identity_reply_adds_strand(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass)
    # connect first, then grab the notification handler
    await hass.services.async_call(DOMAIN, "send_raw", {"p1": 1, "p2": 1, "p3": 1}, blocking=True)
    notify = client.start_notify.call_args_list[-1].args[1]
    plain = bytes([0x09, 0x00, 0x73, 0x3E]) + b"LAB" + bytes.fromhex("00001A92A5") + b"\0\0"
    pkt = make_packet(derive_key(""), 5, plain, source=0x0009)
    char_c = MagicMock(uuid=CHAR_CONTINUE)
    char_d = MagicMock(uuid=CHAR_COMPLETE)
    notify(char_c, bytearray(pkt[:20]))
    notify(char_d, bytearray(pkt[20:]))
    await hass.async_block_till_done()
    assert entry.data[CONF_STRANDS]["LAB00001A92A5"] == 9
    assert er.async_get(hass).async_get_entity_id("light", DOMAIN, "LAB00001A92A5")
    assert entry.state.value == "loaded"


async def test_first_setup_runs_discovery(hass: HomeAssistant, client) -> None:
    with patch("custom_components.showhome.mesh.asyncio.sleep", AsyncMock()):
        await _setup(hass, strands={})
        await hass.async_block_till_done()
    sent = _sent(client)
    assert len(sent) == 2 and all(p[3] & 0xF == 0xE for p in sent)
    # connects to the strongest light first
    assert client.establish.call_args.args[2] == "LA3000014F9EC"


async def test_via_and_diagnostics(hass: HomeAssistant, client) -> None:
    from custom_components.showhome.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass)
    await hass.services.async_call(
        DOMAIN, "send_raw", {"p1": 1, "p2": 1, "p3": 3, "via": "92A5"}, blocking=True
    )
    assert client.establish.call_args.args[2] == "LAB00001A92A5"
    with patch("custom_components.showhome.diagnostics.bluetooth.async_discovered_service_info", return_value=[]):
        diag = await async_get_config_entry_diagnostics(hass, entry)
    kinds = [h["kind"] for h in diag["history"]]
    assert "connected" in kinds and kinds[-1] == "tx"
    assert diag["history"][-1]["via"] == "LAB00001A92A5"


async def test_empty_service_cache_is_cleared_and_retried(hass: HomeAssistant) -> None:
    """Through a proxy, the first connection can return an empty service list.
    The mesh must clear the cache and reconnect, not crash the service call."""
    from custom_components.showhome.csrmesh import CHAR_COMPLETE, CHAR_CONTINUE

    good_char = lambda u, p: MagicMock(uuid=u, handle=1, properties=p)
    good = MagicMock(uuid=SERVICE_UUID)
    good.characteristics = [good_char(CHAR_CONTINUE, ["write-without-response"]),
                            good_char(CHAR_COMPLETE, ["write", "notify"])]

    class Services(list):
        def get_characteristic(self, uuid):
            for svc in self:
                for c in svc.characteristics:
                    if c.uuid == uuid:
                        return c
            return None

    class FakeClient:
        def __init__(self):
            self.is_connected = True
            self.clear_cache_count = 0
            self.writes = []
            self._disconnected = False
        @property
        def services(self):
            return Services([]) if self.clear_cache_count == 0 else Services([good])
        async def start_notify(self, *a):
            pass
        async def clear_cache(self):
            self.clear_cache_count += 1
        async def disconnect(self):
            pass
        async def write_gatt_char(self, char, data, response=True):
            self.writes.append((char, bytes(data)))

    client = FakeClient()

    with patch("custom_components.showhome.mesh.establish_connection", AsyncMock(return_value=client)):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_PIN: "", CONF_STRANDS: {"LA3000014F9EC": 5}})
        entry.add_to_hass(hass)
        with patch(
            "custom_components.showhome.mesh.bluetooth.async_discovered_service_info",
            return_value=[_info()],
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await hass.services.async_call(DOMAIN, "send_raw", {"p1": 1, "p2": 1, "p3": 3}, blocking=True)

    assert client.clear_cache_count == 1   # cache was refreshed once
    # a 27-byte packet splits into two chunk writes; the last is on CHAR_COMPLETE
    assert client.writes and client.writes[-1][0] == CHAR_COMPLETE


async def test_unreachable_lights_raise_clean_error(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError
    from bleak.exc import BleakError

    with patch(
        "custom_components.showhome.mesh.bluetooth.async_discovered_service_info",
        return_value=[_info()],
    ), patch(
        "custom_components.showhome.config_flow.async_discovered_service_info", return_value=[_info()]
    ), patch(
        "custom_components.showhome.mesh.establish_connection",
        AsyncMock(side_effect=BleakError("gatt error")),
    ):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_PIN: "", CONF_STRANDS: {"LA3000014F9EC": 5}})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "send_raw", {"p1": 1, "p2": 1, "p3": 3}, blocking=True)


async def test_password_from_options_is_used(hass: HomeAssistant, client) -> None:
    """A password set via the options flow must be what encrypts commands."""
    from custom_components.showhome.csrmesh import derive_key

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PIN: "", CONF_STRANDS: {"LA3000014F9EC": 5}},
        options={"pin": "holiday1", "brightness_levels": 10},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "send_raw", {"p1": 1, "p2": 1, "p3": 3}, blocking=True)
    # packets must decrypt with the options password, not the blank data one
    assert _sent(client, "holiday1")
    assert entry.runtime_data._key == derive_key("holiday1")


async def test_find_key_detects_the_working_formula(hass: HomeAssistant) -> None:
    """find_key must report the formula whose key the lights actually answer to."""
    from custom_components.showhome.csrmesh import (
        CHAR_COMPLETE, CHAR_CONTINUE, SERVICE_UUID, key_candidates, make_packet, open_packet,
    )

    pin = "holiday1"
    target_name, target_key = key_candidates(pin)[3]   # the light answers only to this one

    char_c = MagicMock(uuid=CHAR_CONTINUE, handle=1, properties=["write-without-response", "write", "notify"])
    char_d = MagicMock(uuid=CHAR_COMPLETE, handle=2, properties=["write-without-response", "write", "notify"])
    svc = MagicMock(uuid=SERVICE_UUID)
    svc.characteristics = [char_c, char_d]

    class Services(list):
        def get_characteristic(self, uuid):
            return {CHAR_CONTINUE: char_c, CHAR_COMPLETE: char_d}.get(uuid)

    class FakeLight:
        """Replies to a discovery iff it was encrypted with target_key."""
        def __init__(self):
            self.is_connected = True
            self._notify = {}
            self._buf = b""
            self.services = Services([svc])
        async def start_notify(self, char, cb):
            self._notify[char] = cb
        async def disconnect(self):
            self.is_connected = False
        async def clear_cache(self):
            pass
        async def write_gatt_char(self, char, data, response=True):
            self._buf += bytes(data)
            if char != CHAR_COMPLETE:
                return
            packet, self._buf = self._buf, b""
            if open_packet(target_key, packet) is None:
                return   # wrong key -> silent, like the real lights
            reply = bytes([0x11, 0x00, 0x73, 0x2E]) + b"LAB" + bytes.fromhex("DE6114BB3F")
            pkt = make_packet(target_key, 7, reply, source=0x0011)
            self._notify[CHAR_CONTINUE](MagicMock(uuid=CHAR_CONTINUE), bytearray(pkt[:20]))
            self._notify[CHAR_COMPLETE](MagicMock(uuid=CHAR_COMPLETE), bytearray(pkt[20:]))

    light = FakeLight()
    with patch("custom_components.showhome.mesh.establish_connection", AsyncMock(return_value=light)), patch(
        "custom_components.showhome.mesh.bluetooth.async_discovered_service_info", return_value=[_info()]
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_PIN: "", CONF_STRANDS: {"LA3000014F9EC": 5}}, options={"pin": pin}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.services.async_call(DOMAIN, "find_key", {}, blocking=True, return_response=True)

    assert result["found"] == target_name
    assert entry.runtime_data._key == target_key   # mesh keeps the working key
