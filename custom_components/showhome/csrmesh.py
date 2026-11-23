"""CSRmesh packet crypto and Holiday ShowHome command format.

Pure functions only (no Home Assistant imports) so they can be unit tested.

Protocol notes (from https://github.com/craus81/Christmas-lights, which is based on
https://github.com/nkaminski/csrmesh):

* key  = first 16 bytes of reversed SHA-256(PIN + b"\\0MCP")
* packet = seq(3, LE) | source(2, LE) | AES-128-OFB(plaintext) | MAC(8) | 0xFF
* IV  = seq(3) | 0x00 | source(2) | 0x00 * 10
* MAC = first 8 bytes of reversed HMAC-SHA256(key, 0x00*8 | seq | source | ciphertext)
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUID = "0000fef1-0000-1000-8000-00805f9b34fb"
CHAR_CONTINUE = "c4edc000-9daf-11e3-8003-00025b000b00"
CHAR_COMPLETE = "c4edc000-9daf-11e3-8004-00025b000b00"

SOURCE_ID = 0x8000
BROADCAST = 0x0000

MODE_COLOR = 1
MODE_EFFECT = 2

# Name prefix (first three characters) -> product, as shown in the Show App.
PRODUCTS = {
    "LA3": "Icicles",
    "LA4": "Snowflake",
    "LA7": "Rope Light",
    "LAB": "LED Ball",
    "LAC": "Tree",
}


def product_for(name: str) -> str:
    """Return a friendly product name for an advertised name like LAB00001A92A5."""
    return PRODUCTS.get(name[:3].upper(), "Light")


def short_code(name: str) -> str:
    """Return the 4 character code the Show App shows (last 4 characters)."""
    return name[-4:].upper()


def derive_key(pin: str) -> bytes:
    digest = bytearray(hashlib.sha256(pin.encode() + b"\x00MCP").digest())
    digest.reverse()
    return bytes(digest[:16])


def _keystream_xor(key: bytes, iv: bytes, data: bytes) -> bytes:
    # OFB with this IV: keystream block n = AES^n(IV). Implemented over ECB so we
    # don't depend on OFB being available in every cryptography release.
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = bytearray()
    block = iv
    for i in range(0, len(data), 16):
        block = enc.update(block)
        out += bytes(a ^ b for a, b in zip(data[i : i + 16], block))
    return bytes(out)


def _iv(seq: bytes, src: bytes) -> bytes:
    return seq + b"\x00" + src + b"\x00" * 10


def _mac(key: bytes, seq: bytes, src: bytes, ct: bytes) -> bytes:
    digest = bytearray(hmac.new(key, b"\x00" * 8 + seq + src + ct, hashlib.sha256).digest())
    digest.reverse()
    return bytes(digest[:8])


def make_packet(key: bytes, seq_num: int, plain: bytes, source: int = SOURCE_ID) -> bytes:
    seq = (seq_num & 0xFFFFFF).to_bytes(3, "little")
    src = source.to_bytes(2, "little")
    ct = _keystream_xor(key, _iv(seq, src), plain)
    return seq + src + ct + _mac(key, seq, src, ct) + b"\xff"


@dataclass(frozen=True)
class MeshMessage:
    source: int
    payload: bytes


def open_packet(key: bytes, packet: bytes) -> MeshMessage | None:
    """Verify and decrypt a packet. Returns None when the MAC does not match."""
    for body in (packet[:-1], packet):
        if len(body) < 13:
            continue
        seq, src, ct, mac = body[:3], body[3:5], body[5:-8], body[-8:]
        if hmac.compare_digest(_mac(key, seq, src, ct), mac):
            return MeshMessage(int.from_bytes(src, "little"), _keystream_xor(key, _iv(seq, src), ct))
    return None


def split_for_write(packet: bytes) -> list[tuple[str, bytes]]:
    """Split a packet into (characteristic, chunk) writes (MTL framing)."""
    if len(packet) > 20:
        return [(CHAR_CONTINUE, packet[:20]), (CHAR_COMPLETE, packet[20:])]
    return [(CHAR_COMPLETE, packet)]


class CommandBuilder:
    """Builds ShowHome plaintext commands; keeps the rolling 4-bit counter."""

    def __init__(self, counter: int = 0) -> None:
        self._counter = counter & 0xF

    def _next(self, low: int) -> int:
        self._counter = (self._counter + 1) & 0xF
        return (self._counter << 4) | low

    def setting(self, dest: int, p1: int, p2: int, p3: int) -> bytes:
        b = self._next(3)
        return bytes([dest & 0xFF, (dest >> 8) & 0xFF, 0x73, b, 0, 0xFF, 0xFE, b, p1 & 0xFF, p2 & 0xFF, p3 & 0xFF, 0, 0])

    def discover(self) -> bytes:
        return bytes([0, 0, 0x73, self._next(0xE), 0, 0xFF, 0xFF, 0, 0, 0, 0, 0, 0])


def parse_identity(payload: bytes) -> str | None:
    """Return the light's name (e.g. LA7000036DF87) from a discovery reply."""
    if len(payload) >= 12 and payload[2] == 0x73 and (payload[3] & 0xF) == 0xE and payload[4:6] == b"LA":
        return payload[4:7].decode("ascii", "replace") + payload[7:12].hex().upper()
    return None
