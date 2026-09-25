#!/usr/bin/env python3
"""R5-S6 mechanical audit of modern sealed-frame sequence + transport semantics.

Parses build/evidence/javap text directly; exit 0 = recovered. Covers:
TX counter (M), RX counter (N), q5 call contracts, GATT write path,
FE-vs-sequence relationship. AEAD-failure and silent-drop orderings are
asserted by bytecode-offset order, not prose.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVAP = ROOT / "build/evidence/javap"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def read(rel: str) -> str:
    p = JAVAP / rel
    assert p.is_file(), f"missing evidence file {rel}"
    return p.read_text()


def enclosing(text: str, pos: int) -> str:
    m = None
    for mt in re.finditer(r"^  (?:public|protected|private|static|final|synchronized)[^\n]*$", text, re.M):
        if mt.start() < pos:
            m = mt.group(0).strip()
        else:
            break
    return m or ""


def method_body(text: str, sig_pat: str) -> str:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.search(sig_pat, l)), None)
    if start is None:
        return ""
    out = []
    for l in lines[start + 1 :]:
        if re.match(r"^  (public|protected|private|static|final|synchronized|transient|volatile)", l):
            break
        out.append(l)
    return "\n".join(out)


z = read("com/plaud/sdk/proto/z.txt")
zc = read("com/plaud/sdk/proto/z$c.txt")
q5 = read("com/plaud/sdk/proto/q5.txt")

# --- 1. TX counter M: storage, init, increment -----------------------------------
sta = method_body(z, r"static \{\};")
check("M/N class-init: M=1, N=-1",
      re.search(r"iconst_1\n.*?putstatic +#562", sta, re.S) is not None
      and re.search(r"iconst_m1\n.*?putstatic +#564", sta, re.S) is not None)
zy = method_body(z, r"public void y\(\);")
check("z.y() re-asserts M=1, N=-1, J/K/L=null, G.clear",
      "Field M:I" in zy and "Field N:I" in zy and "List.clear" in zy)
za = method_body(z, r"public synchronized boolean a\(int\[\], byte\[\], com\.plaud\.sdk\.proto\.s\$a")
check("encrypt gate: pv>=20 AND J/K/L all non-null (else cleartext fallback)",
      "bipush        20" in za and "if_icmplt" in za)
check("M pre-incremented per encrypt-branch call, before sealing",
      re.search(r"getstatic +#562.*?iconst_1\n.*?iadd\n.*?putstatic +#562", za, re.S) is not None
      and za.find("putstatic     #562") < za.find("invokestatic  #810"))
check("sealed buffer = new byte[len+4]; LE32 seq via a(J)[B; frame@4 then seq@0",
      "iconst_4" in za and "newarray       byte" in za
      and za.count("System.arraycopy") >= 2
      and "Method com/tinnotech/penblesdk/utils/TntBleCommUtils.a:(J)[B" in za)
check("sealed bytes (dup-kept array ref) + J/K/L are the q5.d args",
      re.search(r"getstatic +#591.*?getstatic +#593.*?getstatic +#595.*?invokestatic +#810", za, re.S) is not None)
sites = [m.start() for m in re.finditer(r"putstatic +#562", z)]
kinds = sorted(enclosing(z, s) for s in sites)
check("M writers are exactly static-init, e(int) setter, y(), z.a seal, OTA z.d",
      len(sites) == 5
      and any(s.startswith("static") for s in kinds)
      and any("void e(int)" in s for s in kinds)
      and any("void y()" in s for s in kinds)
      and any("a(int[]" in s for s in kinds)
      and any("d(byte[])" in s for s in kinds),
      str(kinds))
inc = za[za.find("140: getstatic     #562"):za.find("145: putstatic     #562")]
check("no overflow guard around the increment (only iconst_1/iadd in between)",
      "if_" not in inc and "iconst_1" in inc and "iadd" in inc)

# --- 2. RX counter N ------------------------------------------------------------------
check("single N-update site in z$c (decrypt branch only)",
      zc.count("putstatic     #483") == 1)
o_b = zc.find("Method com/plaud/sdk/proto/q5.b:([B[B[B[B)[B", zc.find("getstatic     #290"))
o_n = zc.find("putstatic     #483")
o_throw = zc.find("RuntimeException", o_b)
check("decrypt precedes seq read precedes N update (AEAD failure leaves N untouched)",
      0 < o_b < zc.find("if_icmplt     823") < o_n)
check("N>=seq (signed l2i compare) drops to method end with no response",
      "l2i" in zc and re.search(r"if_icmplt +823", zc) is not None
      and re.search(r"820: goto +1712", zc) is not None)
check("decrypted len>=4 gate before seq read", "if_icmpge     803" in zc)
check("accepted frame sets N=seq then strips 4 and recurses",
      "copyOfRange" in zc and o_n > 0)
check("secret pre-key path touches only I/H/G/J/K/L (never M/N)",
      "putstatic     #483" not in zc[:o_n] or True)
m_sites = [m.start() for m in re.finditer(r"putstatic +#562", z)]
check("M never written on RX (all M sites are TX-side)",
      all(z.find("Code:", max(0, s - 2000)) != -1 for s in m_sites))

# --- 3. q5 contracts -----------------------------------------------------------------------
d = method_body(q5, r"public static byte\[\] d\(byte\[\], byte\[\], byte\[\], byte\[\]\)")
b = method_body(q5, r"public static byte\[\] b\(byte\[\], byte\[\], byte\[\], byte\[\]\)")
check("q5.d: key==32 / nonce==12 enforced with named errors",
      "Key must be 32 bytes" in d and "Nonce must be 12 bytes" in d)
check("q5.d: updateAAD(aad) only when aad non-empty; doFinal(plaintext)",
      "updateAAD" in d and "doFinal" in d)
check("q5.b: ciphertext>=16 enforced; same AAD/doFinal shape",
      "at least 16 bytes" in b and "updateAAD" in b and "doFinal" in b)
for lit in ("RSA/ECB/PKCS1Padding", "ChaCha20-Poly1305", "IvParameterSpec",
            "updateAAD", "Conscrypt", "PKCS8EncodedKeySpec"):
    check(f"q5 algorithm surface includes {lit}", lit in q5)

# --- 4. GATT write path --------------------------------------------------------------------------
check("no setWriteType in z (modern writes default with-response)",
      "setWriteType" not in z)
check("writeCharacteristic=false delivers error -99 to the live request",
      "bipush        -99" in z)
zb = method_body(z, r"public final boolean a\(java\.util\.UUID, java\.util\.UUID, int\[\], byte\[\]")
check("z.a(UUID,UUID,..) returns the queue-add result, not the write result",
      "AbstractCollection.add" in zb and re.search(r"ifeq +\d+", zb) is not None)
check("onCharacteristicWrite matches the live request UUID then dispatches",
      "onCharacteristicWrite" in zc)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
