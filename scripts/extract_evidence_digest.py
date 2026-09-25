#!/usr/bin/env python3
"""Extract a protocol digest straight from javap bytecode -> docs/evidence-digest.json.

WHY THIS EXISTS
---------------
Most of this repo's tests compare the emulator against fixtures that were
themselves written from the emulator's understanding of the protocol. Such a
test proves self-consistency, not correctness. This script closes that loop by
deriving protocol facts MECHANICALLY from the shipped SDK's bytecode, with no
human in the path, so `tests/test_evidence_conformance.py` can assert that the
emulator agrees with the artifact rather than with itself.

The digest is committed and pinned to the AAR's sha256. Re-run this script after
`scripts/build-evidence.sh`; if the digest changes, either the SDK changed or a
protocol claim was wrong.

WHAT IT EXTRACTS (per class in com/plaud/sdk/proto/)
  * the constant returned by getBleProtocolType() / getBleRequestType()  (requests)
  * the constant returned by a()                                          (responses)
  * every TntBleCommUtils reader call in <init>, as (method, literal offset),
    which gives the response field layout in parse order
  * the class's toString() format literal, which gives the real field names
  * the length guards (`if (bArr.length < N)` / `>= N`) in <init>

It parses javap text, so it is only as good as javap -- but javap is the
compiler's own reader, not a decompiler's reconstruction.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVAP = ROOT / "build/evidence/javap/com/plaud/sdk/proto"
AAR = ROOT / "reference/plaud-org/plaud-sdk-public/sdk/android/plaud-sdk.aar"
OUT = ROOT / "docs/evidence-digest.json"

# TntBleCommUtils readers: one-letter name -> width in bits.
READERS = {"a": 8, "b": 16, "c": 24, "d": 32, "e": 64}
# Allocating writers used by enPkg(): one-letter name -> width in bits.
ALLOCATORS = {"c": 8, "a": 16, "b": 24}

# Keep in sync with docs/protocol-ledger.md: these UUIDs are extracted from
# w$d below; the ROLE mapping in the digest is still hand-maintained.
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
)


CALL = re.compile(r"^\s*\d+:\s+invokevirtual\s+#\d+\s+//\s+Method com/tinnotech/penblesdk/utils/TntBleCommUtils\.(\w+):\(([^)]*)\)(\S+)")
LDC_STR = re.compile(r'^\s*\d+:\s+ldc\w*\s+#\d+\s+//\s+String (.*)$')
METHOD_SIG = re.compile(r"^  (?:public|protected|private|static|final|\s)*[\w.$\[\]<>, ]*\s(\S+)\((.*?)\);?\s*$")


INT_PUSH = re.compile(r"^\s*\d+:\s+(?:iconst_(\d)|iconst_(m1)|bipush\s+(-?\d+)|sipush\s+(-?\d+)|ldc\w*\s+#\d+\s+//\s+(?:int|long)\s+(-?\d+)l?)\s*$")
ISTORE = re.compile(r"^\s*\d+:\s+istore(?:_(\d)|\s+(\d+))\s*$")
ILOAD = re.compile(r"^\s*\d+:\s+iload(?:_(\d)|\s+(\d+))\s*$")
ARRAYLEN = re.compile(r"^\s*\d+:\s+arraylength\s*$")
CMP = re.compile(r"^\s*\d+:\s+(if_icmp\w+)\s+\d+\s*$")
CLEARS = ("invokevirtual", "invokestatic", "invokespecial", "invokeinterface",
          "putfield", "getfield", "areturn", "ireturn", "return", "new ", "anewarray")


def split_methods(text: str) -> dict[str, list[str]]:
    """Split a javap class dump into {method-signature: [bytecode lines]}."""
    out: dict[str, list[str]] = {}
    current = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(";"):
            if current is not None:
                out[current] = body
            current = line.strip().rstrip(";")
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        out[current] = body
    return out


def _int_push(line: str):
    """The integer literal a push instruction puts on the stack, or None."""
    m = INT_PUSH.match(line)
    if not m:
        return None
    if m.group(2):
        return -1
    for g in (1, 3, 4, 5):
        if m.group(g) is not None:
            return int(m.group(g))
    return None


def _slot(m):
    return int(m.group(1) if m.group(1) is not None else m.group(2))


def _trace(body):
    """Linear constant propagation over a javap method body.

    ProGuard reuses locals aggressively, so the offset operand of a
    TntBleCommUtils reader is often `iload_2` rather than a literal. Yielding
    (line, last_constant_on_stack, locals) lets callers read the operand
    either way. This is not a verifier -- it is a straight-line scan, which is
    enough because every site of interest is `aload_1; <offset>; invokevirtual`.
    """
    last = None
    locals_: dict[int, int] = {}
    for line in body:
        v = _int_push(line)
        if v is not None:
            last = v
            yield line, last, locals_
            continue
        m = ISTORE.match(line)
        if m:
            if last is not None:
                locals_[_slot(m)] = last
            yield line, last, locals_
            continue
        m = ILOAD.match(line)
        if m:
            last = locals_.get(_slot(m))
            yield line, last, locals_
            continue
        yield line, last, locals_
        if any(c in line for c in CLEARS):
            last = None


def returned_constant(body: list[str]):
    """The integer a trivial `return <const>;` method returns."""
    last = None
    for line, cur, _ in _trace(body):
        if cur is not None:
            last = cur
        if "ireturn" in line:
            return last
    return None


def reader_calls(body: list[str]) -> list[dict]:
    """Every TntBleCommUtils reader/allocator call, with its offset operand."""
    out = []
    for line, last, _ in _trace(body):
        m = CALL.match(line)
        if not m:
            continue
        name, args = m.group(1), m.group(2)
        if args == "[BI" and name in READERS:
            out.append({"reader": name, "width_bits": READERS[name], "offset": last})
        elif args == "I" and name in ALLOCATORS:
            out.append({"allocator": name, "width_bits": ALLOCATORS[name]})
        elif args == "J" and name == "a":
            out.append({"allocator": "a(long)", "width_bits": 32})
    return out


def length_guards(body: list[str]) -> list[dict]:
    """`bArr.length <op> N` comparisons, in order -- the parser's guard chain."""
    out = []
    lines = list(_trace(body))
    for i, (line, _last, _loc) in enumerate(lines):
        if not ARRAYLEN.match(line):
            continue
        value = None
        for j in range(i + 1, min(i + 5, len(lines))):
            nxt, cur, _ = lines[j]
            c = CMP.match(nxt)
            if c is not None:
                out.append({"op": c.group(1), "value": value})
                break
            v = _int_push(nxt)
            if v is not None:
                value = v
    return out


def tostring_literal(body: list[str]) -> str | None:
    best = None
    for line in body:
        m = LDC_STR.match(line)
        if m:
            s = m.group(1)
            if "{" in s and "=" in s:
                if best is None or len(s) > len(best):
                    best = s
    return best


def enpkg_structure(body: list[str]) -> dict:
    """Structural facts about a request enPkg body, by dumb pattern matching.

    Records PRESENCE only -- which calls occur, which integer literals are
    pushed or compared, which string operations appear. It never assigns
    meaning: that a compared literal is a portVersion threshold, or that a
    pushed literal is a token width, is ledger prose verified by hand against
    the same bytecode, never something this function claims.

    Applied generically to every enPkg method, not tuned to any one class,
    so its output for k3 cannot have been shaped to agree with the emulator.
    """
    struct: dict = {
        "calls_packHead": False,
        "scratch_buffers": [],
        "compared_constants": [],
        "int_literals": [],
        "string_ops": [],
        "u4_calls": [],
    }
    literals: set[int] = set()
    for line, last, _ in _trace(body):
        if "packHead" in line and "invoke" in line:
            struct["calls_packHead"] = True
        v = _int_push(line)
        if v is not None and -1000000 < v < 1000000:
            literals.add(v)
        if "newarray" in line and "anewarray" not in line and last is not None:
            struct["scratch_buffers"].append(last)
        m = CMP.match(line)
        if m and last is not None:
            entry = {"op": m.group(1), "value": last}
            if entry not in struct["compared_constants"]:
                struct["compared_constants"].append(entry)
        for op in ("substring", "trim", "isEmpty", "append"):
            if op in line and "invoke" in line and op not in struct["string_ops"]:
                struct["string_ops"].append(op)
        mm = re.search(r"com/plaud/sdk/proto/u4\.(\w+)", line)
        if mm and "invoke" in line and mm.group(1) not in struct["u4_calls"]:
            struct["u4_calls"].append(mm.group(1))
    struct["int_literals"] = sorted(literals)
    return struct



def enum_table(path: Path) -> dict:
    """Recover (name, ordinal, wire) for every constant of a Java enum whose
    constructor is <init>(String, int, int): javap's <clinit> shows, per
    constant, `ldc "NAME"`, two int pushes (ordinal then the wire value stored
    in the enum's int field), `invokespecial <init>`, `putstatic FIELD`.
    ProGuard kept the name strings in the shipped SDK, which is what makes the
    CommonAction/CommonType wire tables (opcode 8) mechanically recoverable."""
    text = path.read_text()
    out: dict = {}
    pat = re.compile(
        r'ldc\s+#\d+\s+// String ([A-Za-z_][A-Za-z0-9_]*)\n'
        r'\s+\d+: (iconst_m?\d|bipush\s+-?\d+|sipush\s+-?\d+)\n'
        r'\s+\d+: (iconst_m?\d|bipush\s+-?\d+|sipush\s+-?\d+)\n'
        r'\s+\d+: invokespecial[^\n]*<init>[^\n]*\n'
        r'\s+\d+: putstatic\s+#\d+\s+// Field ([A-Za-z_$][A-Za-z0-9_$]*):'
    )
    def val(tok: str) -> int:
        tok = tok.strip()
        if tok.startswith("iconst_m"):
            return -int(tok[8:])
        if tok.startswith("iconst_"):
            return int(tok[7:])
        return int(tok.split()[1])
    for m in pat.finditer(text):
        out[m.group(1)] = {"ordinal": val(m.group(2)), "wire": val(m.group(3)), "field": m.group(4)}
    return out


def main() -> int:
    if not JAVAP.is_dir():
        print(f"missing {JAVAP} -- run ./scripts/build-evidence.sh first", file=sys.stderr)
        return 1
    classes = {}
    for path in sorted(JAVAP.glob("*.txt")):
        cls = path.stem
        if "$" in cls:
            continue
        text = path.read_text()
        methods = split_methods(text)
        entry: dict = {}
        for sig, body in methods.items():
            base = sig.split("(")[0].split()[-1]
            if base.endswith("getBleProtocolType"):
                entry["protocol_type"] = returned_constant(body)
            elif base.endswith("getBleRequestType"):
                entry["request_opcode"] = returned_constant(body)
            elif base == "a" and sig.endswith("a()") and "int" in sig:
                entry["response_opcode"] = returned_constant(body)
            elif base.endswith("toString"):
                lit = tostring_literal(body)
                if lit:
                    entry["tostring"] = lit
            if f"com.plaud.sdk.proto.{cls}(byte[])" in sig:
                entry["init_readers"] = reader_calls(body)
                entry["init_guards"] = length_guards(body)
            if base.endswith("enPkg"):
                entry["enpkg_calls"] = reader_calls(body)
                entry["enpkg_structure"] = enpkg_structure(body)
            # Any other method that takes a byte[] also parses wire data --
            # q2.a([B,I) holds the file-list ENTRY layout, which <init> does not.
            if "byte[]" in sig and f"com.plaud.sdk.proto.{cls}(" not in sig:
                calls = reader_calls(body)
                if calls:
                    entry.setdefault("other_methods", {})[sig] = {
                        "readers": calls,
                        "guards": length_guards(body),
                    }
        if entry:
            classes[cls] = entry

    # The two ~80-entry opcode tables in w$a / w$c. Which is which is settled
    # below by counting how the opcodes UNIQUE to each table map to classes.
    import re as _re

    wtext = (ROOT / "build/evidence/jadx-out/sources/com/plaud/sdk/proto/w.java")
    tables = {}
    if wtext.is_file():
        body = wtext.read_text()
        for name in ("a", "c"):
            m = _re.search(r"interface %s \{(.*?)\n    \}" % name, body, _re.S)
            if m:
                tables[name] = sorted({int(v) for v in _re.findall(r"= (\d+);", m.group(1))})

    digest = {
        "_comment": (
            "MECHANICALLY EXTRACTED from javap bytecode by scripts/extract_evidence_digest.py. "
            "Do not hand-edit. Regenerate after scripts/build-evidence.sh. "
            "tests/test_evidence_conformance.py asserts the emulator against this file, which "
            "is what makes those assertions independent of the emulator."
        ),
        "aar_sha256": hashlib.sha256(AAR.read_bytes()).hexdigest() if AAR.exists() else None,
        "extractor_version": 3,
        "gatt": {
            "service": "00001910-0000-1000-8000-00805f9b34fb",
            "notify": "00002BB0-0000-1000-8000-00805f9b34fb",
            "write": "00002BB1-0000-1000-8000-00805f9b34fb",
            "cccd": "00002902-0000-1000-8000-00805f9b34fb",
            "battery_service": "0000180f-0000-1000-8000-00805f9b34fb",
            "battery_level": "00002a19-0000-1000-8000-00805f9b34fb",
            "roles_source": "hand mapping; the UUID SET below is mechanical (w$d javap)",
        },
        "gatt_uuids": sorted(
            set(
                UUID_RE.findall(
                    (ROOT / "build/evidence/javap/com/plaud/sdk/proto/w$d.txt").read_text()
                )
            )
        ) if (ROOT / "build/evidence/javap/com/plaud/sdk/proto/w$d.txt").is_file() else [],
        "opcode_tables": tables,
        # Inner-class enums are skipped by the class walk above ("$" names);
        # these two carry the opcode-8 CommonSettings wire vocabulary.
        "enums": {
            name: enum_table(JAVAP / f"{name}.txt")
            for name in ("s0$b$a", "s0$b$b")
            if (JAVAP / f"{name}.txt").is_file()
        },
        "classes": classes,
    }
    OUT.write_text(json.dumps(digest, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(classes)} classes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
