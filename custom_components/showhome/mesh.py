"""BLE link to the ShowHome CSRmesh network.

All lights form one mesh, so a single connection to any light (the strongest one
HA can hear, through any Bluetooth proxy) reaches every strand.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import time
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
        self._pin = pin
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
        # Ring buffer of BLE traffic and events, exposed through diagnostics.
        self.history: deque[dict] = deque(maxlen=200)
        self.gatt_services: list[dict] = []
        self._via: str | None = None
        self._verified_count = 0

    def _log(self, kind: str, **data) -> None:
        self.history.append({"t": round(time.time(), 3), "kind": kind, **data})

    @property
    def connected_name(self) -> str | None:
        return self._connected_name if self._client and self._client.is_connected else None

    def set_identity_callback(self, cb: Callable[[int, str], None]) -> None:
        self._identity_cb = cb

    def _candidates(self) -> list[bluetooth.BluetoothServiceInfoBleak]:
        infos = [i for i in bluetooth.async_discovered_service_info(self.hass, connectable=True) if is_showhome(i)]
        if self._via:
            via = self._via.upper()
            infos = [i for i in infos if via in (i.address.upper(), (i.name or "").upper()) or (i.name or "").upper().endswith(via)]
        return sorted(infos, key=lambda i: i.rssi, reverse=True)

    async def async_use_via(self, via: str | None) -> None:
        """Force the link through one light (name, last-4 code or address)."""
        via = via or None
        if via != self._via:
            self._via = via
            await self.async_disconnect()

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client and self._client.is_connected:
            return self._client
        candidates = self._candidates()
        if not candidates:
            self._log("error", msg=f"no candidates (via={self._via})")
            raise MeshError(f"No ShowHome lights in range of Home Assistant's Bluetooth (via={self._via})")
        last: Exception | None = None
        for info in candidates[:3]:
            try:
                client = await self._connect_one(info)
            except (BleakError, BleakNotFoundError, TimeoutError) as err:
                _LOGGER.debug("Could not connect to %s: %s", info.name, err)
                self._log("connect_failed", name=info.name, address=info.address, error=repr(err))
                last = err
                continue
            self._client = client
            self._connected_name = info.name
            return client
        raise MeshError(f"Could not connect to any ShowHome light: {last}")

    async def _connect_one(self, info: bluetooth.BluetoothServiceInfoBleak) -> BleakClientWithServiceCache:
        """Connect to one light, making sure the control service is really there.

        Through a proxy, establish_connection can hand back a cached, empty
        service list, so the FEF1 characteristics look missing. When that
        happens, clear the cache and rediscover once before giving up.
        """
        client: BleakClientWithServiceCache | None = None
        for attempt in (1, 2):
            client = await establish_connection(
                BleakClientWithServiceCache,
                info.device,
                info.name or info.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=2,
            )
            self.gatt_services = [
                {
                    "uuid": svc.uuid,
                    "chars": [
                        {"uuid": c.uuid, "handle": c.handle, "props": list(c.properties)}
                        for c in svc.characteristics
                    ],
                }
                for svc in client.services
            ]
            if self._has_controls(client):
                break
            self._log("services_incomplete", name=info.name, attempt=attempt,
                      services=[s["uuid"] for s in self.gatt_services])
            if attempt == 1:
                await client.clear_cache()
                await client.disconnect()
                continue
            await client.disconnect()
            raise BleakError(
                f"{info.name}: control service {SERVICE_UUID} not found after cache refresh"
            )

        assert client is not None
        notify = []
        for char in (CHAR_CONTINUE, CHAR_COMPLETE):
            try:
                await client.start_notify(char, self._on_notify)
                notify.append(char)
            except BleakError as err:
                _LOGGER.debug("Notifications unavailable on %s: %s", char, err)
                self._log("notify_failed", char=char, error=repr(err))
        self._log("connected", name=info.name, address=info.address, rssi=info.rssi,
                  source=getattr(info, "source", None), notify=notify)
        _LOGGER.debug("Connected to ShowHome mesh via %s (%s dBm)", info.name, info.rssi)
        return client

    @staticmethod
    def _has_controls(client: BleakClientWithServiceCache) -> bool:
        try:
            return all(client.services.get_characteristic(c) is not None for c in (CHAR_CONTINUE, CHAR_COMPLETE))
        except BleakError:
            return False

    @callback
    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("ShowHome mesh disconnected")
        self._client = None
        self._rx.clear()
        self._log("disconnected")

    def _on_notify(self, char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._rx += data
        self._log("rx_chunk", char=char.uuid, data=bytes(data).hex())
        if char.uuid.lower() != CHAR_COMPLETE:
            return
        packet, self._rx = bytes(self._rx), bytearray()
        msg = open_packet(self._key, packet)
        if msg is None:
            _LOGGER.debug("Unreadable mesh packet (wrong password?): %s", packet.hex())
            self._log("rx_unreadable", packet=packet.hex())
            return
        self._verified_count += 1
        self._log("rx", source=f"{msg.source:04x}", payload=msg.payload.hex(" "))
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
            packet = make_packet(self._key, self._seq, plain)
            for char, chunk in split_for_write(packet):
                await client.write_gatt_char(char, chunk, response=self._wants_response(client, char))
            _LOGGER.debug("Mesh tx: %s", plain.hex(" "))
            self._log("tx", via=self._connected_name, plain=plain.hex(" "), packet=packet.hex())
            self._schedule_idle_disconnect()

    @staticmethod
    def _wants_response(client: BleakClientWithServiceCache, uuid: str) -> bool:
        try:
            char = client.services.get_characteristic(uuid)
        except Exception:  # noqa: BLE001
            char = None
        if char is None or not isinstance(getattr(char, "properties", None), (list, tuple, set)):
            return True
        return "write" in char.properties

    async def async_send_setting(self, dest: int, p1: int, p2: int, p3: int) -> None:
        try:
            await self._send(self._cmd.setting(dest, p1, p2, p3))
        except (BleakError, TimeoutError) as err:
            # One retry on a fresh connection; links through proxies drop easily.
            _LOGGER.debug("Send failed (%s), reconnecting", err)
            self._log("send_failed", error=repr(err))
            await self.async_disconnect()
            try:
                await self._send(self._cmd.setting(dest, p1, p2, p3))
            except (BleakError, TimeoutError) as err2:
                self._log("send_failed", error=repr(err2))
                raise MeshError(f"Could not send to the lights: {err2}") from err2

    async def async_discover(self) -> None:
        """Ask every light to report its mesh ID and name (replies are async)."""
        try:
            await self._send(self._cmd.discover())
            await asyncio.sleep(1.5)
            await self._send(self._cmd.discover())
        except (BleakError, TimeoutError) as err:
            self._log("send_failed", error=repr(err))
            await self.async_disconnect()
            raise MeshError(f"Could not reach the lights to discover them: {err}") from err

    async def async_find_key(self, via: str | None = None) -> dict:
        """Try each standard key derivation and see which one the lights answer.

        For the one configured password, derive a key with each published
        formula, send a discovery, and watch for a reply that verifies with
        that same key. The lights only reply when the key is right, so the
        formula that gets a reply is the one the app uses. Reports formula
        names only; the password is never included.
        """
        from .csrmesh import key_candidates, network_tag_candidates

        if via is not None:
            await self.async_use_via(via)

        results: list[dict] = []
        hit: str | None = None
        real_key = self._key
        try:
            for name, key in key_candidates(self._pin):
                self._key = key
                before = self._verified_count
                try:
                    async with self._lock:
                        client = await self._ensure_connected()
                        self._seq = (self._seq + 1) & 0xFFFFFF
                        packet = make_packet(key, self._seq, self._cmd.discover())
                        for char, chunk in split_for_write(packet):
                            await client.write_gatt_char(char, chunk, response=self._wants_response(client, char))
                        self._schedule_idle_disconnect()
                except (BleakError, TimeoutError) as err:
                    results.append({"formula": name, "result": f"send failed: {err}"})
                    continue
                await asyncio.sleep(1.4)
                ok = self._verified_count > before
                results.append({"formula": name, "result": "REPLY — this is the key" if ok else "no reply"})
                if ok:
                    hit = name
                    break
        finally:
            self._key = real_key if hit is None else self._key  # keep the working key if found

        summary = {
            "connected_via": self._connected_name,
            "found": hit,
            "results": results,
            "advertised_tag_hint": "match these against the lights' advertised id (e.g. DE61)",
            "network_tag_candidates": network_tag_candidates(self._pin),
        }
        self._log("find_key", **{k: v for k, v in summary.items() if k != "network_tag_candidates"})
        self._log("network_tags", tags=network_tag_candidates(self._pin))
        return summary

    async def async_read_chars(self, via: str | None = None) -> dict:
        """Read the readable FEF1 characteristics (may expose CSRmesh state)."""
        if via is not None:
            await self.async_use_via(via)
        async with self._lock:
            client = await self._ensure_connected()
            out: dict[str, str] = {}
            for svc in client.services:
                if "fef1" not in svc.uuid.lower():
                    continue
                for ch in svc.characteristics:
                    if "read" in ch.properties:
                        try:
                            out[ch.uuid] = bytes(await client.read_gatt_char(ch)).hex()
                        except BleakError as err:
                            out[ch.uuid] = f"read failed: {err}"
            self._schedule_idle_disconnect()
        self._log("read_chars", chars=out)
        return out

    async def async_disconnect(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        client, self._client = self._client, None
        if client and client.is_connected:
            await client.disconnect()
