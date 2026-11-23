"""BLE link to the ShowHome CSRmesh network.

All lights form one mesh, so a single connection to any light (the strongest one
HA can hear, through any Bluetooth proxy) reaches every strand.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import random

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import IDLE_DISCONNECT_SECONDS
from .csrmesh import (
    CHAR_COMPLETE,
    CHAR_CONTINUE,
    SERVICE_UUID,
    CommandBuilder,
    derive_key,
    make_packet,
    open_packet,
    parse_identity,
    split_for_write,
)

_LOGGER = logging.getLogger(__name__)


class MeshError(Exception):
    """No light in the mesh could be reached."""


def is_showhome(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    return SERVICE_UUID in info.service_uuids and (info.name or "").upper().startswith("LA")


class ShowHomeMesh:
    def __init__(self, hass: HomeAssistant, pin: str) -> None:
        self.hass = hass
        self._key = derive_key(pin)
        self._cmd = CommandBuilder(random.randrange(16))
        self._seq = random.randrange(1, 0xFFFFF)
        self._client: BleakClientWithServiceCache | None = None
        self._connected_name: str | None = None
        self._lock = asyncio.Lock()
        self._rx = bytearray()
        self._idle: asyncio.TimerHandle | None = None
        self._identity_cb: Callable[[int, str], None] | None = None
        self.options_snapshot: dict = {}

    @property
    def connected_name(self) -> str | None:
        return self._connected_name if self._client and self._client.is_connected else None

    def set_identity_callback(self, cb: Callable[[int, str], None]) -> None:
        self._identity_cb = cb

    def _candidates(self) -> list[bluetooth.BluetoothServiceInfoBleak]:
        infos = [i for i in bluetooth.async_discovered_service_info(self.hass, connectable=True) if is_showhome(i)]
        return sorted(infos, key=lambda i: i.rssi, reverse=True)

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client and self._client.is_connected:
            return self._client
        candidates = self._candidates()
        if not candidates:
            raise MeshError("No ShowHome lights are in range of Home Assistant's Bluetooth")
        last: Exception | None = None
        for info in candidates[:3]:
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    info.device,
                    info.name or info.address,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=2,
                )
            except (BleakError, BleakNotFoundError, TimeoutError) as err:
                _LOGGER.debug("Could not connect to %s: %s", info.name, err)
                last = err
                continue
            for char in (CHAR_CONTINUE, CHAR_COMPLETE):
                try:
                    await client.start_notify(char, self._on_notify)
                except BleakError as err:
                    _LOGGER.debug("Notifications unavailable on %s: %s", char, err)
            self._client = client
            self._connected_name = info.name
            _LOGGER.debug("Connected to ShowHome mesh via %s (%s dBm)", info.name, info.rssi)
            return client
        raise MeshError(f"Could not connect to any ShowHome light: {last}")

    @callback
    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("ShowHome mesh disconnected")
        self._client = None
        self._rx.clear()

    def _on_notify(self, char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._rx += data
        if char.uuid.lower() != CHAR_COMPLETE:
            return
        packet, self._rx = bytes(self._rx), bytearray()
        msg = open_packet(self._key, packet)
        if msg is None:
            _LOGGER.debug("Unreadable mesh packet (wrong password?): %s", packet.hex())
            return
        name = parse_identity(msg.payload)
        _LOGGER.debug("Mesh rx from %04x: %s", msg.source, msg.payload.hex(" "))
        if name and self._identity_cb:
            self._identity_cb(msg.source, name)

    def _schedule_idle_disconnect(self) -> None:
        if self._idle:
            self._idle.cancel()
        self._idle = self.hass.loop.call_later(
            IDLE_DISCONNECT_SECONDS, lambda: self.hass.async_create_task(self.async_disconnect())
        )

    async def _send(self, plain: bytes) -> None:
        async with self._lock:
            client = await self._ensure_connected()
            self._seq = (self._seq + 1) & 0xFFFFFF
            for char, chunk in split_for_write(make_packet(self._key, self._seq, plain)):
                await client.write_gatt_char(char, chunk, response=True)
            _LOGGER.debug("Mesh tx: %s", plain.hex(" "))
            self._schedule_idle_disconnect()

    async def async_send_setting(self, dest: int, p1: int, p2: int, p3: int) -> None:
        try:
            await self._send(self._cmd.setting(dest, p1, p2, p3))
        except (BleakError, TimeoutError) as err:
            # One retry on a fresh connection; links through proxies drop easily.
            _LOGGER.debug("Send failed (%s), reconnecting", err)
            await self.async_disconnect()
            await self._send(self._cmd.setting(dest, p1, p2, p3))

    async def async_discover(self) -> None:
        """Ask every light to report its mesh ID and name (replies are async)."""
        await self._send(self._cmd.discover())
        await asyncio.sleep(1.5)
        await self._send(self._cmd.discover())

    async def async_disconnect(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        client, self._client = self._client, None
        if client and client.is_connected:
            await client.disconnect()
