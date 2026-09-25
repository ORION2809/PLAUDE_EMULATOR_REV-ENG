"""The 512-byte PLAUD.AI E2EE recording header and its raw-ChaCha20 payload.

EVIDENCE for the LAYOUT (SOURCE-DERIVED; docs/protocol-ledger.md section 8,
lines 1100-1125, and the constructor `sdk.ble.util.PlaudEncryptHeader(byte[])`
at build/evidence/javap/ALL.txt:125016-125147 which reads, little-endian:
8-byte magic, u16 version, u16 headerSize, u32 crc, 32-byte userId, u16
fileType, u16 channel, u16 encryptType, u32 duration, 70 bytes skipped,
u32 counter, 12-byte nonce, u32 segment, 108-byte algParams, 256-byte
keyCipher; guard "Data must be at least 512 bytes" at ALL.txt:125147;
HEADER_SIZE = 512 and MAGIC_STRING = "PLAUD.AI" at ALL.txt:124983-124986;
`isEncrypted()` at ALL.txt:125195 compares the trimmed magic).

EVIDENCE for the PAYLOAD (SOURCE-DERIVED, ledger "File encryption", lines
1178-1207): raw ChaCha20 (RFC 7539) with NO Poly1305 tag, a fresh engine per
chunk with the same key, same nonce and counter 0; `AudioExporter` skips the
first 512 bytes (ALL.txt:96623) before decrypting; the 32-byte key is the
RSA-unwrapped `keyCipher`. Of the fourteen fields only magic, nonce, segment
and keyCipher have a consumer.

HARNESS_POLICY (values, not layout): the synthetic key and nonce below, a
`keyCipher` that is a labelled filler rather than an RSA-2048 ciphertext (no
Plaud RSA key exists here, and none may be fabricated), version=1,
headerSize=512, crc=0 (never verified by the SDK), fileType=0 and
encryptType=0 (semantics UNKNOWN), counter=0 (never read), segment=0 (so
`AudioDecryptor` and `AudioExporter`, which disagree for segment > 0, agree),
reserved and algParams zero-filled. The realised shape is therefore
"encrypted_ogg" with a harness key: same bytes on the wire as far as the
SDK's two tests can see, but NOT decryptable by any real Plaud key material.
"""

from __future__ import annotations

from dataclasses import dataclass
from struct import pack, unpack_from

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from generator._evidence import plaud_audio as A

HEADER_SIZE = A.RECORDING_HEADER_SIZE  # 512
MAGIC = A.RECORDING_HEADER_MAGIC  # b"PLAUD.AI"

# Field offsets, from the ledger table / the constructor's read order.
OFF_MAGIC, OFF_VERSION, OFF_HEADER_SIZE, OFF_CRC = 0, 8, 10, 12
OFF_USER_ID, OFF_FILE_TYPE, OFF_CHANNEL, OFF_ENCRYPT_TYPE = 16, 48, 50, 52
OFF_DURATION, OFF_RESERVED, OFF_COUNTER, OFF_NONCE = 54, 58, 128, 132
OFF_SEGMENT, OFF_ALG_PARAMS, OFF_KEY_CIPHER = 144, 148, 256

# HARNESS_POLICY: plainly synthetic material, fixed lengths.
SYNTHETIC_KEY = b"SYNTHETIC-E2EE-KEY-0123456789ABC"  # 32 bytes
SYNTHETIC_NONCE = b"SYN-E2EE-NON"  # 12 bytes
SYNTHETIC_USER_ID = b"SYNTHETIC-USER-ID"
SYNTHETIC_KEY_CIPHER = b"SYNTHETIC-KEYCIPHER-NOT-RSA-2048:" + b"\x00" * (256 - 33)
assert len(SYNTHETIC_KEY) == 32 and len(SYNTHETIC_NONCE) == 12 and len(SYNTHETIC_KEY_CIPHER) == 256


@dataclass(frozen=True)
class PlaudEncryptHeader:
    version: int = 1
    header_size: int = HEADER_SIZE
    crc: int = 0
    user_id: bytes = SYNTHETIC_USER_ID
    file_type: int = 0
    channel: int = 1
    encrypt_type: int = 0
    duration_s: int = 0
    reserved: bytes = b"\x00" * 70
    counter: int = 0
    nonce: bytes = SYNTHETIC_NONCE
    segment: int = 0
    alg_params: bytes = b"\x00" * 108
    key_cipher: bytes = SYNTHETIC_KEY_CIPHER

    def pack(self) -> bytes:
        if len(self.nonce) != 12 or len(self.key_cipher) != 256 or len(self.alg_params) != 108 or len(self.reserved) != 70:
            raise ValueError("fixed-size header field has the wrong length")
        if len(self.user_id) > 32:
            raise ValueError("userId is at most 32 bytes")
        out = bytearray(HEADER_SIZE)
        out[OFF_MAGIC : OFF_MAGIC + 8] = MAGIC
        out[OFF_VERSION:OFF_HEADER_SIZE] = pack("<H", self.version)
        out[OFF_HEADER_SIZE:OFF_CRC] = pack("<H", self.header_size)
        out[OFF_CRC:OFF_USER_ID] = pack("<I", self.crc)
        out[OFF_USER_ID : OFF_USER_ID + 32] = self.user_id.ljust(32, b"\x00")
        out[OFF_FILE_TYPE:OFF_CHANNEL] = pack("<H", self.file_type)
        out[OFF_CHANNEL:OFF_ENCRYPT_TYPE] = pack("<H", self.channel)
        out[OFF_ENCRYPT_TYPE:OFF_DURATION] = pack("<H", self.encrypt_type)
        out[OFF_DURATION:OFF_RESERVED] = pack("<I", self.duration_s)
        out[OFF_RESERVED:OFF_COUNTER] = self.reserved
        out[OFF_COUNTER:OFF_NONCE] = pack("<I", self.counter)
        out[OFF_NONCE:OFF_SEGMENT] = self.nonce
        out[OFF_SEGMENT:OFF_ALG_PARAMS] = pack("<I", self.segment)
        out[OFF_ALG_PARAMS:OFF_KEY_CIPHER] = self.alg_params
        out[OFF_KEY_CIPHER:HEADER_SIZE] = self.key_cipher
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes) -> "PlaudEncryptHeader":
        raw = bytes(data)
        if len(raw) < HEADER_SIZE:
            raise ValueError("Data must be at least 512 bytes")  # the SDK's own message
        return cls(
            version=unpack_from("<H", raw, OFF_VERSION)[0],
            header_size=unpack_from("<H", raw, OFF_HEADER_SIZE)[0],
            crc=unpack_from("<I", raw, OFF_CRC)[0],
            user_id=raw[OFF_USER_ID : OFF_USER_ID + 32].rstrip(b"\x00"),
            file_type=unpack_from("<H", raw, OFF_FILE_TYPE)[0],
            channel=unpack_from("<H", raw, OFF_CHANNEL)[0],
            encrypt_type=unpack_from("<H", raw, OFF_ENCRYPT_TYPE)[0],
            duration_s=unpack_from("<I", raw, OFF_DURATION)[0],
            reserved=raw[OFF_RESERVED:OFF_COUNTER],
            counter=unpack_from("<I", raw, OFF_COUNTER)[0],
            nonce=raw[OFF_NONCE:OFF_SEGMENT],
            segment=unpack_from("<I", raw, OFF_SEGMENT)[0],
            alg_params=raw[OFF_ALG_PARAMS:OFF_KEY_CIPHER],
            key_cipher=raw[OFF_KEY_CIPHER:HEADER_SIZE],
        )


def chacha20_raw(key: bytes, nonce: bytes, data: bytes, counter: int = 0) -> bytes:
    """RFC 7539 ChaCha20 keystream XOR, no Poly1305 (ledger section 8).
    `cryptography` takes the 32-bit little-endian block counter prepended to
    the 96-bit nonce; verified against the RFC test vector in the tests."""
    if len(key) != 32:
        raise ValueError("ChaCha20 key must be exactly 32 bytes")  # AudioDecryptor's gate
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")
    full_nonce = int(counter).to_bytes(4, "little") + bytes(nonce)
    return Cipher(algorithms.ChaCha20(bytes(key), full_nonce), mode=None).encryptor().update(bytes(data))


def seal_recording(
    payload: bytes,
    channels: int,
    duration_s: float,
    key: bytes = SYNTHETIC_KEY,
    nonce: bytes = SYNTHETIC_NONCE,
    user_id: bytes = SYNTHETIC_USER_ID,
) -> bytes:
    """header(512) || ChaCha20(key, nonce, counter 0)(payload). One segment."""
    header = PlaudEncryptHeader(channel=channels, duration_s=int(duration_s), nonce=nonce, user_id=user_id)
    return header.pack() + chacha20_raw(key, nonce, payload, counter=0)


def open_recording(data: bytes, key: bytes) -> tuple[PlaudEncryptHeader, bytes]:
    """Skip 512 bytes, decrypt with the header's nonce (single segment)."""
    raw = bytes(data)
    info = A.classify_recording(raw)
    if not info["encrypted"]:
        raise ValueError("not an E2EE recording (no PLAUD.AI header)")
    header = PlaudEncryptHeader.unpack(raw)
    plain = chacha20_raw(key, header.nonce, raw[HEADER_SIZE:], counter=0)
    return header, plain


__all__ = [
    "HEADER_SIZE",
    "MAGIC",
    "PlaudEncryptHeader",
    "SYNTHETIC_KEY",
    "SYNTHETIC_KEY_CIPHER",
    "SYNTHETIC_NONCE",
    "SYNTHETIC_USER_ID",
    "chacha20_raw",
    "open_recording",
    "seal_recording",
]
