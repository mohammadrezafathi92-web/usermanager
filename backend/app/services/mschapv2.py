"""MS-CHAPv2 (RFC 2759) + MPPE key derivation (RFC 3079) + the RADIUS
Microsoft VSA wire format (RFC 2548) - everything radius_server.py needs to
accept L2TP/SSTP/IKEv2 clients, which (unlike the OpenVPN client this panel
already worked with) commonly hard-code MS-CHAPv2 with no PAP/CHAP fallback
(SSTP on Windows in particular always speaks MS-CHAPv2).

This is a from-scratch, pure-Python implementation (no external MS-CHAP
library exists on PyPI that's worth the extra dependency) written directly
against RFC 2759/3079/2548 - it has NOT been tested against a live MikroTik
router (no such router is reachable from where this code was written), so
treat the first real-world attempt as a test run: radius_server.py logs
plenty of detail on failure to make debugging that first attempt quick.

MD4 is implemented by hand (see md4() below) rather than via hashlib,
because hashlib's MD4 support depends on the underlying OpenSSL build
having the legacy provider enabled - frequently NOT the case on modern
slim Docker images - and MS-CHAPv2 cannot work at all without it.
"""
from __future__ import annotations

import hashlib
import os
import struct
import warnings

# cryptography deprecated TripleDES (still functional as of the pinned
# 42.0.7) - we deliberately still use it below (see _des_encrypt) since
# there is no other DES primitive available without adding a whole new
# dependency just for this one operation. Silence the one-time warning so
# it doesn't spam the RADIUS auth log on every single login.
warnings.filterwarnings("ignore", message=".*TripleDES.*")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

MS_VENDOR_ID = 311
MS_CHAP_CHALLENGE = 11
MS_CHAP2_RESPONSE = 25
MS_CHAP2_SUCCESS = 26
MS_MPPE_SEND_KEY = 16
MS_MPPE_RECV_KEY = 17


# --------------------------------------------------------------------- MD4
def md4(data: bytes) -> bytes:
    def lrot(x: int, n: int) -> int:
        x &= 0xFFFFFFFF
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def F(x, y, z):
        return (x & y) | (~x & z & 0xFFFFFFFF)

    def G(x, y, z):
        return (x & y) | (x & z) | (y & z)

    def H(x, y, z):
        return x ^ y ^ z

    msg = bytearray(data)
    orig_len_bits = (len(msg) * 8) & 0xFFFFFFFFFFFFFFFF
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", orig_len_bits)

    h0, h1, h2, h3 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476

    K1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    K2 = [0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15]
    K3 = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]
    S1 = [3, 7, 11, 19]
    S2 = [3, 5, 9, 13]
    S3 = [3, 9, 11, 15]

    for off in range(0, len(msg), 64):
        X = list(struct.unpack("<16I", bytes(msg[off:off + 64])))
        A, B, C, D = h0, h1, h2, h3

        def round_(A, B, C, D, func, keys, shifts, const):
            for i, k in enumerate(keys):
                s = shifts[i % 4]
                x = X[k]
                if i % 4 == 0:
                    A = lrot((A + func(B, C, D) + x + const) & 0xFFFFFFFF, s)
                elif i % 4 == 1:
                    D = lrot((D + func(A, B, C) + x + const) & 0xFFFFFFFF, s)
                elif i % 4 == 2:
                    C = lrot((C + func(D, A, B) + x + const) & 0xFFFFFFFF, s)
                else:
                    B = lrot((B + func(C, D, A) + x + const) & 0xFFFFFFFF, s)
            return A, B, C, D

        A, B, C, D = round_(A, B, C, D, F, K1, S1, 0x00000000)
        A, B, C, D = round_(A, B, C, D, G, K2, S2, 0x5A827999)
        A, B, C, D = round_(A, B, C, D, H, K3, S3, 0x6ED9EBA1)

        h0 = (h0 + A) & 0xFFFFFFFF
        h1 = (h1 + B) & 0xFFFFFFFF
        h2 = (h2 + C) & 0xFFFFFFFF
        h3 = (h3 + D) & 0xFFFFFFFF

    return struct.pack("<4I", h0, h1, h2, h3)


def _md4_selftest() -> None:
    vectors = {
        b"": "31d6cfe0d16ae931b73c59d7e0c089c0",
        b"a": "bde52cb31de33e46245e05fbdbd6fb24",
        b"abc": "a448017aaf21d8525fc10ae87aa6729d",
        b"message digest": "d9130a8164549fe818874806e1c7014b",
        b"abcdefghijklmnopqrstuvwxyz": "d79e1c308aa5bbcdeea8ed63df412da9",
    }
    for msg, expected in vectors.items():
        got = md4(msg).hex()
        if got != expected:
            raise RuntimeError(
                f"md4 self-test failed for {msg!r}: got {got}, expected {expected} "
                "- MS-CHAPv2 auth (L2TP/SSTP/IKEv2) will not work correctly"
            )


_md4_selftest()


# ---------------------------------------------------------------- DES/3DES
def _des_key_from_7bytes(k7: bytes) -> bytes:
    """Expands a 7-byte (56-bit) key into the 8-byte form DES expects, per
    RFC 2759's key-expansion convention: each output byte holds 7 of the
    input's 56 bits in its top 7 bits, with the low bit left as filler.
    The filler bit's value is irrelevant - real DES implementations
    (including the one behind `cryptography`) never use/check parity, only
    the other 7 bits per byte carry information."""
    bits = []
    for byte in k7[:7]:
        for i in range(8):
            bits.append((byte >> (7 - i)) & 1)
    out = bytearray(8)
    for i in range(8):
        chunk = bits[i * 7:i * 7 + 7]
        val = 0
        for b in chunk:
            val = (val << 1) | b
        val <<= 1
        out[i] = val & 0xFF
    return bytes(out)


def _des_encrypt(key8: bytes, data8: bytes) -> bytes:
    """Single-DES ECB of one 8-byte block. `cryptography` no longer exposes
    single-DES directly (deprecated/removed for being weak on its own), but
    3DES-EDE with all three sub-keys equal is mathematically identical to
    single DES (encrypt-decrypt-encrypt with the same key cancels the
    middle step), which is the standard workaround used wherever only a
    3DES primitive is available."""
    key24 = key8 * 3
    encryptor = Cipher(algorithms.TripleDES(key24), modes.ECB()).encryptor()
    return encryptor.update(data8) + encryptor.finalize()


def _challenge_response(challenge8: bytes, password_hash16: bytes) -> bytes:
    zpwd = password_hash16 + b"\x00" * 5  # 21 bytes total
    k1 = _des_key_from_7bytes(zpwd[0:7])
    k2 = _des_key_from_7bytes(zpwd[7:14])
    k3 = _des_key_from_7bytes(zpwd[14:21])
    return _des_encrypt(k1, challenge8) + _des_encrypt(k2, challenge8) + _des_encrypt(k3, challenge8)


# ------------------------------------------------------------- MS-CHAPv2
def nt_password_hash(password: str) -> bytes:
    return md4(password.encode("utf-16-le"))


def _challenge_hash(peer_challenge16: bytes, auth_challenge16: bytes, username: str) -> bytes:
    # Per RFC 2759, only the account name (no domain\ prefix) is used -
    # this panel's usernames never carry one, so the raw string is fine.
    h = hashlib.sha1()
    h.update(peer_challenge16)
    h.update(auth_challenge16)
    h.update(username.encode("utf-8", "ignore"))
    return h.digest()[:8]


def generate_nt_response(auth_challenge16: bytes, peer_challenge16: bytes, username: str, password: str) -> bytes:
    password_hash = nt_password_hash(password)
    challenge = _challenge_hash(peer_challenge16, auth_challenge16, username)
    return _challenge_response(challenge, password_hash)


_AUTH_MAGIC1 = b"Magic server to client signing constant"  # 39 bytes
_AUTH_MAGIC2 = b"Pad to make it do more than one iteration"  # 41 bytes


def generate_authenticator_response(
    password: str, nt_response24: bytes, peer_challenge16: bytes, auth_challenge16: bytes, username: str
) -> str:
    password_hash = nt_password_hash(password)
    password_hash_hash = md4(password_hash)
    digest = hashlib.sha1(password_hash_hash + nt_response24 + _AUTH_MAGIC1).digest()
    challenge = _challenge_hash(peer_challenge16, auth_challenge16, username)
    final = hashlib.sha1(digest + challenge + _AUTH_MAGIC2).digest()
    return "S=" + final.hex().upper()


# --------------------------------------------------------------- MPPE keys
_MPPE_MASTER_MAGIC1 = b"This is the MPPE Master Key"  # 27 bytes
_MPPE_MAGIC2 = b"On the client side, this is the send key; on the server side, it is the receive key."  # 84 bytes
_MPPE_MAGIC3 = b"On the client side, this is the receive key; on the server side, it is the send key."  # 84 bytes
_SHS_PAD1 = b"\x00" * 40
_SHS_PAD2 = b"\xF2" * 40


def get_master_key(password_hash16: bytes, nt_response24: bytes) -> bytes:
    password_hash_hash = md4(password_hash16)
    return hashlib.sha1(password_hash_hash + nt_response24 + _MPPE_MASTER_MAGIC1).digest()[:16]


def get_asymmetric_start_key(master_key16: bytes, key_len: int, is_send: bool, is_server: bool) -> bytes:
    if is_send:
        magic = _MPPE_MAGIC3 if is_server else _MPPE_MAGIC2
    else:
        magic = _MPPE_MAGIC2 if is_server else _MPPE_MAGIC3
    digest = hashlib.sha1(master_key16 + _SHS_PAD1 + magic + _SHS_PAD2).digest()
    return digest[:key_len]


def get_send_recv_keys(password: str, nt_response24: bytes) -> tuple[bytes, bytes]:
    """Returns (send_key, recv_key) - both 16 bytes - from OUR (the RADIUS
    server / PPP authenticator's) point of view, matching what MS-MPPE-Send-
    Key / MS-MPPE-Recv-Key mean in RFC 2548 (the NAS reads these attribute
    names from ITS OWN point of view too, which is the same "server" role
    here, so no swap is needed when building the RADIUS reply)."""
    password_hash = nt_password_hash(password)
    master_key = get_master_key(password_hash, nt_response24)
    send_key = get_asymmetric_start_key(master_key, 16, is_send=True, is_server=True)
    recv_key = get_asymmetric_start_key(master_key, 16, is_send=False, is_server=True)
    return send_key, recv_key


# ------------------------------------------------------- RADIUS VSA wiring
# Reading incoming Microsoft VSAs needs NO helper at all: pyrad's own
# Packet.DecodePacket already splits each incoming Vendor-Specific(26)
# attribute into the packet dict under a (vendor_id, vendor_type) tuple
# key, value = the raw sub-attribute bytes with the vendor-id/type/length
# header already stripped (see pyrad/packet.py's _PktDecodeVendorAttribute
# and the `if key == 26:` branch in DecodePacket) - this works with the
# plain, unmodified dictionary file already in radius/dictionary (no
# per-vendor ATTRIBUTE/VENDOR entries needed, since pyrad only consults the
# dictionary here to check for TLV-typed sub-attributes, which MS-CHAP's
# aren't). So radius_server.py just does e.g.
# `pkt.get((MS_VENDOR_ID, MS_CHAP2_RESPONSE), [None])[0]` directly.
#
# WRITING a Microsoft VSA back in a reply, on the other hand, needs the
# full raw wire bytes built by hand (build_vsa below) and added under the
# plain "Vendor-Specific" name - pyrad's AddAttribute()/_PktEncodeAttribute
# wrap whatever bytes we hand it in a Type=26/Length header completely
# as-is (see radius_server.py's _check_mschapv2), so build_vsa's output
# (Vendor-Id[4] + Vendor-Type[1] + Vendor-Length[1] + data) is exactly the
# VALUE that call needs.
def build_vsa(vendor_type: int, data: bytes, vendor_id: int = MS_VENDOR_ID) -> bytes:
    return vendor_id.to_bytes(4, "big") + bytes([vendor_type, len(data) + 2]) + data


def encrypt_mppe_key(key: bytes, secret: bytes, request_authenticator: bytes) -> bytes:
    """RFC 2548 section 2.4.2/2.4.3 salted encryption used for
    MS-MPPE-Send-Key / MS-MPPE-Recv-Key attribute values - same
    block-chained MD5-XOR idea as RFC 2865's User-Password encryption, but
    salted and prefixed with a 1-byte key-length octet."""
    salt = bytearray(os.urandom(2))
    salt[0] |= 0x80  # high bit set is mandatory (RFC 2548 2.4.2)
    salt = bytes(salt)

    plain = bytes([len(key)]) + key
    pad_len = (-len(plain)) % 16
    plain += b"\x00" * pad_len

    out = b""
    b = hashlib.md5(secret + request_authenticator + salt).digest()
    for i in range(0, len(plain), 16):
        block = plain[i:i + 16]
        c = bytes(x ^ y for x, y in zip(block, b))
        out += c
        b = hashlib.md5(secret + c).digest()
    return salt + out
