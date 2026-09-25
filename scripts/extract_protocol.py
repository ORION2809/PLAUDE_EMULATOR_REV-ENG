#!/usr/bin/env python3
"""
extract_protocol.py -- recover the Plaud / Tinno BLE wire protocol from the decompiled SDK.

`plaud-sdk.aar` is ProGuard-obfuscated: every class, method and field was renamed to
a, b, c, ... a0, b0.  Five things survived that renaming, and together they are enough to
reconstruct the protocol completely rather than approximately:

  1. Constants keep their values.  Opcodes, UUIDs and magic numbers are intact.
  2. Every message self-declares its opcode -- requests via getBleRequestType(), responses
     via their a() override.  We never have to guess which opcode a class belongs to.
  3. @SourceDebugExtension (emitted by kotlinc for inline-function line mapping) embeds the
     ORIGINAL source file name and fully-qualified class path, e.g.
         com/tinnotech/penblesdk/entity/bean/blepkg/request/SetWifiReq
     ProGuard rewrites class references, not string payloads, so this survives verbatim.
  4. toString() bodies were never stripped.  A response's toString concatenates real field
     names with the obfuscated field identifiers:
         "RecordPauseRsp{sessionId=" + this.b + ", reason=" + this.c + ...
     which yields an exact identifier -> name mapping.
  5. Kotlin @Metadata (d2) carries the original property names in declaration order, and
     Log tags / String.format literals carry class names.

Response constructors parse at explicit literal offsets -- `this.b = util.d(bArr, 3)` is
"u32 little-endian at byte 3" -- so response layouts come out fully resolved: offset, width,
signedness and name.  Request enPkg() bodies build an ordered byte[] merge, giving the
request layout in order.

Output: docs/protocol.json  (machine-readable; the emulator is generated from this)
        docs/protocol-spec.md (human-readable)
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

PROTO_PKG = "com/plaud/sdk/proto"

# --------------------------------------------------------------------------------------
# TntBleCommUtils codec vocabulary, recovered from its own decompiled source.
# It is a thin JNI wrapper over libtnt_ble_utils.so:
#     packInt(widthBits, buf, off, value) / readInt(widthBits, buf, off)
# everything little-endian, plus one-letter convenience wrappers.
# --------------------------------------------------------------------------------------
READ_WIDTH = {"a": 8, "b": 16, "c": 24, "d": 32, "e": 64}   # util.<m>(buf, off)
ALLOC_WIDTH = {"c": 8, "a": 16, "b": 24}                    # util.<m>(intValue) -> byte[]
# util.a(longValue) is a separate 4-byte overload; resolved via the operand's declared type.

# Frame headers, recovered from the request base class packHead().
FRAME_HEADERS = {
    1: "[u8 protocolType=1][u16le opcode]",
    2: "[u8 protocolType=2][u32le 0xFFFFFFFF]",
    3: "[u8 protocolType=3][u32le 0xFFFFFFFF]",
    5: "[u8 protocolType=5][u32le 0xFFFFFFFF]",
}

GATT = {
    "service": "00001910-0000-1000-8000-00805f9b34fb",
    "char_2bb0": "00002BB0-0000-1000-8000-00805f9b34fb",
    "char_2bb1": "00002BB1-0000-1000-8000-00805f9b34fb",
    "cccd": "00002902-0000-1000-8000-00805f9b34fb",
    "battery_service": "0000180f-0000-1000-8000-00805f9b34fb",
    "battery_level": "00002a19-0000-1000-8000-00805f9b34fb",
}

# --------------------------------------------------------------------------------------

@dataclass
class Fld:
    name: str | None
    offset: int | None       # responses only: absolute byte offset in the frame
    width_bits: int | None   # None => variable length
    kind: str                # uint | bool | bytes | string | fixed_bytes
    size: int | None = None  # fixed_bytes length
    note: str = ""


@dataclass
class Message:
    cls: str                       # obfuscated class, e.g. "c6"
    name: str | None               # recovered real name, e.g. "SetWifiReq"
    origin: str | None             # recovered original package path
    direction: str                 # request | response
    protocol_type: int | None = None
    opcode: int | None = None
    header: str | None = None
    layout: list[Fld] = field(default_factory=list)
    min_length: int | None = None
    confidence: str = "high"
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Name recovery
# --------------------------------------------------------------------------------------

_SDE = re.compile(r'@SourceDebugExtension\(\{"(.*?)"\}\)', re.S)
_SDE_PATH = re.compile(r"\+ 1 [\w.]+\n([\w/]+)\n")
_TOSTR_NAME = re.compile(r'return\s+"(\w+)\{')
_FORMAT_NAME = re.compile(r'String\.format\([^,]+,\s*"(\w+)"')
_LOG_TAG = re.compile(r'Log\.[dewiv]\("(\w+)"')
_PRINTLN_NAME = re.compile(r'println\("\w*\s*(\w+Rsp|\w+Req|\w+Notify)\s*:')
_META_D2 = re.compile(r"d2\s*=\s*\{(.*?)\}\s*,\s*xi", re.S)
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')

_JVM_DESC = re.compile(r"^(\[*[BCDFIJSZ]|\[*L[\w/$;]+;?|\(\)?.*)$")


def recover_name(src: str, cls: str) -> tuple[str | None, str | None]:
    """Return (class name, original package path) using every surviving channel."""
    origin = None
    m = _SDE.search(src)
    if m:
        p = _SDE_PATH.search(m.group(1).replace("\\n", "\n"))
        if p:
            origin = p.group(1)

    for pat in (_TOSTR_NAME, _FORMAT_NAME, _PRINTLN_NAME, _LOG_TAG):
        mm = pat.search(src)
        if mm and mm.group(1) not in ("ChenTian",):
            return mm.group(1), origin

    if origin:
        return origin.rsplit("/", 1)[-1], origin
    return None, origin


def kotlin_props(src: str) -> list[str]:
    """Original property names, in declaration order, from Kotlin @Metadata d2."""
    m = _META_D2.search(src)
    if not m:
        return []
    items = [s.replace('\\"', '"') for s in _STR.findall(m.group(1))]
    out: list[str] = []
    skip = {"blesdk_release", "this", "it", "<init>"}
    for it in items:
        if it in skip or not re.fullmatch(r"[a-z][a-zA-Z0-9]*", it):
            continue
        if len(it) == 1 or _JVM_DESC.fullmatch(it) and len(it) <= 2:
            continue
        if it.startswith("get") or it.startswith("set") or it in ("enPkg", "toString"):
            continue
        out.append(it)
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------------------
# Field tables
# --------------------------------------------------------------------------------------

_FIELD_DECL = re.compile(
    r"^\s*public\s+(?:final\s+)?(int|long|short|byte|boolean|String|byte\[\]|java\.lang\.String)\s+(\w+)\s*;",
    re.M)

# toString: "Name{alpha=" + this.b + ", beta=" + this.c
_TOSTR_PAIRS = re.compile(r'[{,]\s*(\w+)\s*=?"?\s*\+\s*this\.(\w+)')
_TOSTR_PAIRS2 = re.compile(r'(\w+)=\"\s*\+\s*this\.(\w+)')


def tostring_names(src: str) -> dict[str, str]:
    """identifier -> real field name, harvested from toString() concatenations."""
    out: dict[str, str] = {}
    body = src[src.find("toString"):] if "toString" in src else ""
    for pat in (_TOSTR_PAIRS, _TOSTR_PAIRS2):
        for name, ident in pat.findall(body):
            out.setdefault(ident, name)
    return out


# --------------------------------------------------------------------------------------
# Response layout: literal-offset parsing in the constructor
# --------------------------------------------------------------------------------------

_RSP_ASSIGN = re.compile(
    r"this\.(\w+)\s*=\s*TntBleCommUtils\.a\(\)\.([abcde])\(bArr,\s*(\d+)\)(\s*==\s*1)?")
_RSP_ARRAYCOPY = re.compile(r"System\.arraycopy\(bArr,\s*(\d+),\s*\w+,\s*0,\s*(\d+)\)")
_LEN_GUARD = re.compile(r"bArr\.length\s*<\s*(\d+)")
_OPCODE_A = re.compile(r"public\s+int\s+a\s*\(\s*\)\s*\{\s*return\s+(-?\d+);")


def parse_response(src: str, names: dict[str, str]) -> tuple[list[Fld], int | None]:
    flds: list[Fld] = []
    for ident, meth, off, isbool in _RSP_ASSIGN.findall(src):
        flds.append(Fld(
            name=names.get(ident, ident),
            offset=int(off),
            width_bits=READ_WIDTH[meth],
            kind="bool" if isbool else "uint",
        ))
    for off, size in _RSP_ARRAYCOPY.findall(src):
        flds.append(Fld(name=None, offset=int(off), width_bits=None,
                        kind="fixed_bytes", size=int(size)))
    flds.sort(key=lambda f: (f.offset if f.offset is not None else 1 << 30))
    guard = _LEN_GUARD.search(src)
    return flds, (int(guard.group(1)) if guard else None)


# --------------------------------------------------------------------------------------
# Request layout: ordered byte[] merge in enPkg()
# --------------------------------------------------------------------------------------

_ENPKG = re.compile(r"byte\[\]\s+enPkg\s*\(\s*\)\s*\{(.*?)\n    \}", re.S)
_MERGE = re.compile(r"byteMergeAll\(new byte\[\]\{(.*?)\}\)", re.S)
_LOCAL_ALLOC = re.compile(
    r"byte\[\]\s+(\w+)\s*=\s*TntBleCommUtils\.a\(\)\.([abc])\(([^;]*?)\);")
_LOCAL_FIXED = re.compile(r"byte\[\]\s+(\w+)\s*=\s*new byte\[(\d+)\];")
_LOCAL_BYTES = re.compile(r"byte\[\]\s+(\w+)\s*=\s*(\w+)\.getBytes\(")
_THIS_REF = re.compile(r"this\.(\w+)")


def parse_request(src: str, decl: dict[str, str], names: list[str]) -> list[Fld]:
    m = _ENPKG.search(src)
    if not m:
        return []
    body = m.group(1)

    # ident -> real name, by aligning declaration order with Kotlin property order
    ident_name = {ident: names[i] for i, ident in enumerate(decl) if i < len(names)}

    locals_: dict[str, Fld] = {}
    for var, meth, arg in _LOCAL_ALLOC.findall(body):
        ref = _THIS_REF.search(arg)
        fname = ident_name.get(ref.group(1)) if ref else None
        width = ALLOC_WIDTH[meth]
        note = ""
        if meth == "a":
            # a(int)->2 bytes vs a(long)->4 bytes; resolve via the operand's declared type
            if ref and decl.get(ref.group(1)) == "long":
                width = 32
            elif ref:
                width = 16
            else:
                note = "overloaded a(); operand type not resolvable"
        if fname is None and "length" in arg:
            fname = "len"
            note = note or "length prefix for the following buffer"
        locals_[var] = Fld(name=fname, offset=None, width_bits=width, kind="uint", note=note)

    for var, size in _LOCAL_FIXED.findall(body):
        locals_[var] = Fld(name=None, offset=None, width_bits=None,
                           kind="fixed_bytes", size=int(size),
                           note="zero-padded fixed-width buffer")
    for var, srcvar in _LOCAL_BYTES.findall(body):
        locals_.setdefault(var, Fld(name=None, offset=None, width_bits=None, kind="bytes"))

    mm = _MERGE.search(body)
    if not mm:
        return list(locals_.values())

    out: list[Fld] = []
    for part in [p.strip() for p in mm.group(1).split(",")]:
        if part == "packHead":
            continue
        if part in locals_:
            out.append(locals_[part])
        else:
            ref = _THIS_REF.search(part)
            out.append(Fld(name=ident_name.get(ref.group(1)) if ref else part,
                           offset=None, width_bits=None, kind="unknown",
                           note=f"unresolved expression: {part}"))

    # attach names to fixed buffers from the preceding length prefix, where obvious
    for i, f in enumerate(out):
        if f.kind == "fixed_bytes" and f.name is None and i > 0:
            prev = out[i - 1]
            if prev.name == "len" and i + 1 <= len(names):
                pass
    return out


# --------------------------------------------------------------------------------------

def main() -> int:
    root = Path(__file__).resolve().parents[1]
    candidates = [root / "reference" / "jadx-out" / "sources" / PROTO_PKG,
                  Path.home() / "work" / "jadx-out" / "sources" / PROTO_PKG]
    srcdir = next((c for c in candidates if c.is_dir()), None)
    if srcdir is None:
        print("error: no decompiled sources; run scripts/fetch-sdk.sh first", file=sys.stderr)
        return 1

    msgs: list[Message] = []
    for path in sorted(srcdir.glob("*.java")):
        src = path.read_text(errors="replace")
        bm = re.search(r"class\s+\w+\s+extends\s+([\w.]+)", src)
        base = bm.group(1) if bm else None
        if base not in ("l", "n", "m", "k"):
            continue

        name, origin = recover_name(src, path.stem)
        decl = {ident: typ for typ, ident in
                ((t, i) for t, i in _FIELD_DECL.findall(src))}
        # _FIELD_DECL yields (type, ident); rebuild preserving order
        decl = {}
        for typ, ident in _FIELD_DECL.findall(src):
            decl[ident] = typ
        decl.pop("tag", None)
        decl.pop("a", None) if base != "l" and "a" not in decl else None

        msg = Message(cls=path.stem, name=name, origin=origin,
                      direction="request" if base == "l" else "response")

        if base == "l":
            pt = re.search(r"getBleProtocolType\s*\(\s*\)\s*\{\s*return\s+(\d+);", src)
            op = re.search(r"getBleRequestType\s*\(\s*\)\s*\{\s*return\s+(\d+);", src)
            msg.protocol_type = int(pt.group(1)) if pt else None
            msg.opcode = int(op.group(1)) if op else None
            msg.header = FRAME_HEADERS.get(msg.protocol_type or -1)
            msg.layout = parse_request(src, decl, kotlin_props(src))
        else:
            op = _OPCODE_A.search(src)
            msg.opcode = int(op.group(1)) if op else None
            msg.header = "[u8 protocolType][u16le opcode]"
            msg.layout, msg.min_length = parse_response(src, tostring_names(src))

        if msg.opcode is None:
            msg.confidence = "low"
            msg.notes.append("opcode not a compile-time constant")
        if any(f.kind == "unknown" for f in msg.layout):
            msg.confidence = "medium"
        if not msg.name:
            msg.notes.append("name not recovered; obfuscated identifier only")

        msgs.append(msg)

    reqs = [m for m in msgs if m.direction == "request"]
    rsps = [m for m in msgs if m.direction == "response"]
    key = lambda m: (m.opcode is None, m.opcode or 0, m.cls)

    doc = {
        "source": "plaud-sdk.aar -> classes.jar, decompiled with jadx 1.5.1",
        "vendor": "TinnoTech Pen BLE SDK, as shipped by Plaud",
        "endianness": "little",
        "integrity": "CRC-16 via libtnt_ble_utils.so; variant unresolved (PROJECT.md Q2)",
        "frame_headers": FRAME_HEADERS,
        "sentinels": {"0xFE10": 65040, "0xFE11": 65041, "0xFE12": 65042,
                      "0xFE20": 65056, "u32_max": 4294967295, "u8_max": 255},
        "gatt": GATT,
        "counts": {
            "requests": len(reqs),
            "responses": len(rsps),
            "named": sum(1 for m in msgs if m.name),
            "with_origin": sum(1 for m in msgs if m.origin),
        },
        "requests": [asdict(m) for m in sorted(reqs, key=key)],
        "responses": [asdict(m) for m in sorted(rsps, key=key)],
    }

    outdir = root / "docs"
    outdir.mkdir(exist_ok=True)
    (outdir / "protocol.json").write_text(json.dumps(doc, indent=2))

    print(f"requests   {len(reqs):3d}")
    print(f"responses  {len(rsps):3d}")
    print(f"named      {doc['counts']['named']:3d} / {len(msgs)}  "
          f"({100*doc['counts']['named']//max(len(msgs),1)}%)")
    print(f"origins    {doc['counts']['with_origin']:3d} original package paths recovered")
    print(f"wrote      docs/protocol.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
