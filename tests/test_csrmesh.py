"""Protocol tests. csrmesh.py has no Home Assistant imports, so load it directly."""

import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:
    from cryptography.hazmat.decrepit.ciphers.modes import OFB
except ImportError:  # older cryptography
    from cryptography.hazmat.primitives.ciphers.modes import OFB

_spec = importlib.util.spec_from_file_location(
    "csrmesh", Path(__file__).parents[1] / "custom_components/showhome/csrmesh.py"
)
cm = importlib.util.module_from_spec(_spec)
sys.modules["csrmesh"] = cm
_spec.loader.exec_module(cm)


def reference_decrypt(pin: str, packet: bytes):
    """Independent port of craus81/Christmas-lights tools/pklg_decode.py."""
    d = bytearray(hashlib.sha256(pin.encode() + b"\x00MCP").digest())
    d.reverse()
    k = bytes(d)[:16]
    body = packet[:-1]
    seq, src, ct, mac = body[:3], body[3:5], body[5:-8], body[-8:]
    h = bytearray(hmac.new(k, b"\0" * 8 + seq + src + ct, hashlib.sha256).digest())
    h.reverse()
    assert bytes(h)[:8] == mac, "MAC mismatch"
    dec = Cipher(algorithms.AES(k), OFB(seq + b"\0" + src + b"\0" * 10)).decryptor()
    return int.from_bytes(src, "little"), dec.update(ct) + dec.finalize()


def test_packet_matches_reference_implementation():
    for pin in ("", "1234", "my lights"):
        builder = cm.CommandBuilder(0)
        plain = builder.setting(0, 1, 1, 3)
        pkt = cm.make_packet(cm.derive_key(pin), 0x123456, plain)
        assert pkt[:3] == bytes([0x56, 0x34, 0x12])
        assert pkt[3:5] == b"\x00\x80"  # source 0x8000, little endian
        assert pkt[-1] == 0xFF
        assert len(pkt) == 3 + 2 + len(plain) + 8 + 1
        assert reference_decrypt(pin, pkt) == (0x8000, plain)


def test_long_payload_uses_multiple_aes_blocks():
    plain = bytes(range(40))
    pkt = cm.make_packet(cm.derive_key(""), 7, plain)
    assert reference_decrypt("", pkt)[1] == plain
    assert cm.open_packet(cm.derive_key(""), pkt).payload == plain


def test_open_packet_round_trip_and_wrong_pin():
    key = cm.derive_key("")
    pkt = cm.make_packet(key, 99, b"hello mesh!!!", source=0x0042)
    msg = cm.open_packet(key, pkt)
    assert msg == cm.MeshMessage(0x0042, b"hello mesh!!!")
    assert cm.open_packet(key, pkt[:-1]) == msg  # without the trailing 0xFF
    assert cm.open_packet(cm.derive_key("wrong"), pkt) is None


def test_setting_command_layout():
    b = cm.CommandBuilder(0)
    cmd = b.setting(0x1234, 1, 2, 1)
    assert cmd == bytes([0x34, 0x12, 0x73, 0x13, 0, 0xFF, 0xFE, 0x13, 1, 2, 1, 0, 0])
    # counter rolls over after 16
    for _ in range(15):
        cmd = b.setting(0, 1, 1, 1)
    assert cmd[3] == 0x03 and cmd[7] == 0x03


def test_discover_command_layout():
    cmd = cm.CommandBuilder(4).discover()
    assert cmd == bytes([0, 0, 0x73, 0x5E, 0, 0xFF, 0xFF, 0, 0, 0, 0, 0, 0])


def test_split_for_write():
    pkt = cm.make_packet(cm.derive_key(""), 1, cm.CommandBuilder().setting(0, 1, 1, 1))
    parts = cm.split_for_write(pkt)
    assert len(pkt) == 27
    assert parts == [(cm.CHAR_CONTINUE, pkt[:20]), (cm.CHAR_COMPLETE, pkt[20:])]
    assert cm.split_for_write(b"x" * 20) == [(cm.CHAR_COMPLETE, b"x" * 20)]


def test_parse_identity_matches_advertised_names():
    # Reply layout: src(2) 73 Xe 'L' 'A' <type> <5 bytes id>
    payload = bytes([0x01, 0x00, 0x73, 0x2E]) + b"LA3" + bytes.fromhex("000014F9EC") + b"\0\0"
    assert cm.parse_identity(payload) == "LA3000014F9EC"
    payload = bytes([0x01, 0x00, 0x73, 0x2E]) + b"LAB" + bytes.fromhex("00001A92A5")
    assert cm.parse_identity(payload) == "LAB00001A92A5"
    assert cm.parse_identity(bytes([0, 0, 0x73, 0x23]) + b"LA3" + bytes(5)) is None


def test_product_names():
    assert (cm.product_for("LA3000014F9EC"), cm.short_code("LA3000014F9EC")) == ("Icicles", "F9EC")
    assert cm.product_for("LA4000019B542") == "Snowflake"
    assert cm.product_for("LAB00001A92A5") == "LED Ball"
    assert cm.product_for("LAC0000139A2F") == "Tree"
    assert cm.product_for("LAZ0000000000") == "Light"
