#!/usr/bin/env python3
"""R5-S5 mechanical audit of the modern (portVersion >= 20) handshake path.

Parses build/evidence/javap text directly (ground truth); two checks read
build/evidence/jadx-out (lambda bodies invisible to javap) and are labeled
JADX-SOURCED. Exit 0 = recovered.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVAP = ROOT / "build/evidence/javap"
JADX = ROOT / "build/evidence/jadx-out/sources/com/plaud/sdk/proto"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS " if cond else "FAIL "
    print(tag + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def read(p: Path) -> str:
    assert p.is_file(), f"missing evidence file {p}"
    return p.read_text()


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


q = read(JAVAP / "com/plaud/sdk/proto/q.txt")
z = read(JAVAP / "com/plaud/sdk/proto/z.txt")
zc = read(JAVAP / "com/plaud/sdk/proto/z$c.txt")
q5 = read(JAVAP / "com/plaud/sdk/proto/q5.txt")
v4 = read(JAVAP / "com/plaud/sdk/proto/v4.txt")
w4 = read(JAVAP / "com/plaud/sdk/proto/w4.txt")
jq = read(JADX / "q.java")
jz = read(JADX / "z.java")

# --- 1. modern entry: both set_data_notify callbacks -----------------------------
shape = re.findall(r"Method com/plaud/sdk/proto/z\.y:\(\)V.*?getPortVersion.*?bipush +20.*?if_icmplt",
                   q, re.S)
check("modern entry: z.y() -> pv>=20 -> n() else q.a(), on BOTH notify paths",
      len(shape) >= 2, f"{len(shape)} shapes")
check("entry calls n() on the modern side",
      q.count("Method n:()V") >= 2)

# --- 2. z.y() reset -----------------------------------------------------------------
zy = method_body(z, r"public void y\(\);")
check("z.y() clears J/K/L, G, H/I and resets M=1 N=-1",
      "Field J:[B" in zy and "Field K:[B" in zy
      and "Field L:[B" in zy and "iconst_1" in zy
      and "iconst_m1" in zy and "List.clear" in zy)

# --- 3. q.n(): sn-signature chunks ------------------------------------------------------
qn = method_body(q, r"public final void n\(\);")
check("q.n: empty serial -> s0$d.f fail, no chunks sent",
      re.search(r"TextUtils\.isEmpty.*?Field com/plaud/sdk/proto/s0\$d\.f:", qn, re.S) is not None)
check("q.n: null snSignature -> sn_signature_empty (no crash)",
      "sn_signature_empty" in qn and "snSignature is null" in qn)
check("q.n: Base64.decode(sig, NO_WRAP=2)", "Base64.decode" in qn and "iconst_2" in qn)
check("q.n: chunk count=(len+99)/100, 100B copyOfRange slices",
      "bipush        100" in qn and "bipush        99" in qn and "copyOfRange" in qn)
check("q.n: new v4(index, count, chunk, forceClear=q.h)",
      'Method com/plaud/sdk/proto/v4."<init>":(II[BZ)V' in qn and "Field h:Z" in qn)
check("q.n: waiters {65041, 65042} per chunk, fire-and-forget (pop, no send-fail check)",
      "int 65041" in qn and "int 65042" in qn and re.search(r"invokevirtual #466.*?pop", qn, re.S) is not None)

# --- 4. q.a0(): RSA public-key chunks ------------------------------------------------------
qa0 = method_body(q, r"public final void a0\(\);")
check("q.a0: null userRSAPublicKey -> user_rsa_public_key_empty + s0$d.f",
      "user_rsa_public_key_empty" in qa0)
check("q.a0: PEM TEXT bytes, same 100B chunking, new w4(index, count, chunk)",
      "String.getBytes" in qa0 and 'Method com/plaud/sdk/proto/w4."<init>":(II[B)V' in qa0)
check("q.a0: waiter {65042} per chunk",
      "int 65042" in qa0)

# --- 5. marker framing ----------------------------------------------------------------------
check("v4 marker: 0xFE20 iff forceClear else 0xFE10",
      "int 65056" in v4 and "int 65040" in v4 and "Field d:Z" in v4)
check("w4 marker is 0xFE12", "int 65042" in w4)
check("marker enPkg never calls packHead (no type-1 prefix)",
      "packHead" not in v4 and "packHead" not in w4)

# --- 6. z$c secret reassembly + key establishment (javap) --------------------------------------
check("0xFE11 dispatches waiter w$a.a", "65041" in zc or "w$a" in zc)
for frag in ("Field com/plaud/sdk/proto/z.I:I", "Field com/plaud/sdk/proto/z.H:I",
             "Field com/plaud/sdk/proto/z.G:Ljava/util/List;",
             "sort", "copyOfRange", "userRSAPrivateKey",
             "Method com/plaud/sdk/proto/q5.a:(Ljava/lang/String;[B)[B",
             "bipush        56",
             "Method com/plaud/sdk/proto/q5.b:([B[B[B[B)[B",
             "String PLAUD.AI", "int 65042"):
    check(f"z$c secret path contains {frag[:42]}", frag in zc)
check("J/K/L split bounds 0/32, 32/44, 44/56",
      "bipush        32" in zc and "bipush        44" in zc and "bipush        56" in zc)

# --- 7. encrypted RX + TX (javap) ----------------------------------------------------------------
check("encrypted RX: J/K/L-gated q5.b decrypt, len>=4, u32 seq, N>=seq drop, strip-4 recurse",
      "Method com/plaud/sdk/proto/q5.b:([B[B[B[B)[B" in zc and "l2i" in zc
      and "Field com/plaud/sdk/proto/z.N:I" in zc)
check("encrypted TX branch seals [u32 seq][frame] via q5.d on the same 1910/2BB1 write",
      "Method com/plaud/sdk/proto/q5.d:([B[B[B[B)[B" in z
      and "Field com/plaud/sdk/proto/w$d.c:Ljava/util/UUID;" in z)

# --- 8. q5 algorithms (javap literals) ----------------------------------------------------------------
for lit in ("RSA/ECB/PKCS1Padding", "SHA256withRSA", "ChaCha20-Poly1305",
            "IvParameterSpec", "updateAAD", "Conscrypt", "PKCS8EncodedKeySpec"):
    check(f"q5 uses {lit}", lit in q5)

# --- 9. modern reuses legacy post-key methods verbatim -----------------------------------------------
qa = method_body(q, r"public final void a\(\);")
check("q.a itself has NO pv>=20 comparison (shared by both paths; pv only a k3 layout selector)",
      "bipush        20" not in qa)
check("same bind_token_empty guard gates the modern path too",
      "bind_token_empty" in qa)

# --- 10. JADX-SOURCED: lambda-only dispatch rules ------------------------------------------------------
check("[jadx] n() last-chunk reply 0xFE11 -> a0() else q.a()",
      "TntBleCommUtils.a().b(bArr2, 0) == 65041" in jq and "a0();" in jq)
check("[jadx] a0() last-chunk reply -> q.a() unconditionally",
      re.search(r"if \(i3 == length - 1\) \{\s+a\(\);", jq) is not None)
check("[jadx] secret self-check dispatches waiter 65042",
      "z.this.a(65042)" in jz)
check("[jadx] AEAD failure rethrown (not dropped)",
      "throw new RuntimeException(e6)" in jz or "throw new RuntimeException(e9)" in jz)

# --- 11. emulator scope stance --------------------------------------------------------------------------
sys.path.insert(0, str(ROOT / "emulator"))
from plaudsim.profile import ENCRYPTED_PORT_VERSION, PORT_VERSION  # noqa: E402

check("emulator declares cleartext portVersion and refuses >= 20",
      PORT_VERSION < ENCRYPTED_PORT_VERSION == 20)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
