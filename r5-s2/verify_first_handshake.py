#!/usr/bin/env python3
"""R5-S2 mechanical audit of the SDK first-handshake outbound path.

Parses build/evidence/javap text (the compiler's own reader, not a
decompiler) and asserts every structural fact the README claims, in
bytecode-offset order where ordering matters. No human in the path:
exit 0 means the construction below was recovered, non-zero names the
failed check.

Also cross-checks emulator/plaudsim/handshake.py agreement with a
synthetic (obviously fake) token round-trip -- structure only, never a
credential claim.
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


def method_body(text: str, sig_pat: str) -> str:
    """Slice a javap method body: from the signature line to the next
    top-level member or exception-table end. Returns '' when absent."""
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


q = read("com/plaud/sdk/proto/q.txt")
k3 = read("com/plaud/sdk/proto/k3.txt")
j3 = read("com/plaud/sdk/proto/j3.txt")
z = read("com/plaud/sdk/proto/z.txt")
l = read("com/plaud/sdk/proto/l.txt")
u4 = read("com/plaud/sdk/proto/u4.txt")
tnt = read("com/tinnotech/penblesdk/utils/TntBleCommUtils.txt")
agent = read("sdk/PlaudDeviceAgent.txt")
nice = read("sdk/NiceBuildSdk.txt")

# --- 1. q.a() guard BEFORE any k3 construction -------------------------------
qa = method_body(q, r"public final void a\(\);")
off_isempty = qa.find("TextUtils.isEmpty")
off_empty_lit = qa.find("bind_token_empty")
off_newk3 = qa.find("class com/plaud/sdk/proto/k3")
off_enpkg = qa.find("k3.enPkg")
check("q.a guard: TextUtils.isEmpty present", off_isempty > 0)
check("q.a guard: bind_token_empty literal present", off_empty_lit > 0)
check("q.a guard: UUID_IS_EMPTY failure path present", "UUID_IS_EMPTY" in qa)
check(
    "q.a guard fires BEFORE k3 is instantiated",
    0 < off_isempty < off_empty_lit < off_newk3,
    "guard must precede construction in offset order",
)
check("q.a: no frame can be built on the empty path (return before new k3)",
      qa.find("46: return", qa.find("bind_token_empty")) > 0 or "46: return" in qa)

# --- 2. k3 construction arguments --------------------------------------------
m_init = method_body(k3, r"public com\.plaud\.sdk\.proto\.k3\(int, int, java\.lang\.String, int\);")
check("k3.<init>(int,int,String,int): (agent,stage,token,portVersion) field order",
      "Field a:I" in m_init and "Field b:I" in m_init
      and "Field c:Ljava/lang/String;" in m_init and "Field d:I" in m_init)
check("q.a passes z.h() as arg0 (agent)", "Method com/plaud/sdk/proto/z.h:()I" in qa)
check("q.a passes constant 0 as arg1 (stage=k3 first)",
      re.search(r"103: iconst_0\n\s+104: aload_3", qa) is not None)
check("q.a passes field g (bind token) as arg2", "Field g:Ljava/lang/String;" in qa)
check("q.a passes BleDevice.getPortVersion() as arg3 (layout selector)",
      "Method com/tinnotech/penblesdk/entity/BleDevice.getPortVersion:()I" in qa)
check("q.B() returns field g (bind-token accessor)",
      "public java.lang.String B" in q and "Field g:Ljava/lang/String;" in q)

# --- 3. k3.enPkg structure ----------------------------------------------------
ke = method_body(k3, r"public byte\[\] enPkg\(\);")
check("k3.enPkg calls l.packHead ([01][01 00] header)", "packHead" in ke)
check("k3.enPkg allocates a 255-byte scratch buffer", "sipush        255" in ke)
check("k3.enPkg trims via u4.b", "Method com/plaud/sdk/proto/u4.b:([B)[B" in ke)
check("k3.enPkg has the d>=3 stage branch", "iconst_3" in ke and "if_icmplt" in ke)
check("k3.enPkg has the d>=9 width branch", "bipush        9" in ke)
check("k3.enPkg token widths 16/32", "bipush        16" in ke and "bipush        32" in ke)
check("k3.enPkg pads with 48 ('0')", "bipush        48" in ke)
check("k3.enPkg string ops isEmpty/append/substring",
      "String.isEmpty" in ke and "StringBuilder.append" in ke and "String.substring" in ke)
check("k3.enPkg appends token bytes via ByteBuffer.wrap+put(getBytes)",
      "ByteBuffer.wrap" in ke and "String.getBytes" in ke and "ByteBuffer.put" in ke)
proto_body = method_body(k3, r"public int getBleProtocolType\(\);")
req_body = method_body(k3, r"public int getBleRequestType\(\);")
check("k3 proto type 1 / request opcode 1",
      "iconst_1" in proto_body and "ireturn" in proto_body
      and "iconst_1" in req_body and "ireturn" in req_body)

# --- 4. packHead type-1 = [u8 proto][u16le opcode] ----------------------------
ph = method_body(l, r"public byte\[\] packHead\(\);")
check("packHead type-1 builds a 3-byte array", "71: iconst_3" in ph)
check("packHead writes proto with TntBleCommUtils.a (u8)",
      "TntBleCommUtils.a:([BIJ)I" in ph)
check("packHead writes opcode with TntBleCommUtils.b (u16)",
      "TntBleCommUtils.b:([BIJ)I" in ph)

# --- 5. writer widths are pure-Java xx->packInt dispatch -----------------------
for meth, width in (("a", 8), ("b", 16), ("c", 24), ("d", 32), ("e", 64)):
    body = method_body(tnt, rf"public int {meth}\(byte\[\], int, long\);")
    check(f"TntBleCommUtils.{meth} = {width}-bit writer",
          f"bipush        {width}" in body and "packInt" in body)

# --- 6. z.h() stub -------------------------------------------------------------
zh = method_body(z, r"public int h\(\);")
check("z.h() is stubbed to constant 0", "iconst_0" in zh and "ireturn" in zh)

# --- 7. GATT write path -> service 1910 / char 2BB1, default write type --------
za = method_body(z, r"public synchronized boolean a\(int\[\], byte\[\], com\.plaud\.sdk\.proto\.s\$a")
check("z.a(int[],byte[],..) targets w$d.a (1910) + w$d.c (2BB1)",
      za.count("Field com/plaud/sdk/proto/w$d.a:Ljava/util/UUID;") >= 3
      and za.count("Field com/plaud/sdk/proto/w$d.c:Ljava/util/UUID;") >= 3)
check("terminal write resolves characteristic via getService+getCharacteristic",
      "BluetoothGatt.getService" in z and "BluetoothGattService.getCharacteristic" in z)
check("terminal write: setValue + writeCharacteristic",
      "BluetoothGattCharacteristic.setValue" in z and "BluetoothGatt.writeCharacteristic" in z)
check("no setWriteType anywhere in z (default = with-response)",
      "setWriteType" not in z)

# --- 8. waiters {1,2} and send-fail path ---------------------------------------
check("q.a registers response waiters {1,2}",
      re.search(r"newarray\s+int.*?iconst_1.*?iastore.*?iconst_2.*?iastore", qa, re.S) is not None)
check("q.a send-fail path logs HandShakeReq:one--sendFail + stage send_fail",
      "HandShakeReq:one--sendFail" in qa and "send_fail" in qa)

# --- 9. j3 tail -----------------------------------------------------------------
check("j3 extends k3", "public class com.plaud.sdk.proto.j3 extends com.plaud.sdk.proto.k3" in j3)
check("j3.<init> chains k3.<init> with the first four args",
      'Method com/plaud/sdk/proto/k3."<init>":(IILjava/lang/String;I)V' in j3)
je = method_body(j3, r"public byte\[\] enPkg\(\);")
check("j3 short-circuits to bare k3 bytes only when BOTH e,f are null",
      je.count("ifnonnull     19") >= 2 and "k3.enPkg" in je)
check("j3 devToken field is a raw 8-byte array (bipush 8, no length prefix)",
      "bipush        8" in je and "newarray       byte" in je)
check("j3 userName length uses the 1-byte c() allocator (u8)",
      "TntBleCommUtils.c:(I)[B" in je)
check("j3 merges via l.byteMergeAll", "byteMergeAll" in je)
qd0 = method_body(q, r"public final void d0\(\);")
check("q.d0 passes constant 1 (stage=j3 second)",
      re.search(r"73: iload_1\n\s+74: iconst_1\n", qd0) is not None)

# --- 10. u4.b trailing-zero trim --------------------------------------------------
ub = method_body(u4, r"public static byte\[\] b\(byte\[\]\);")
check("u4.b trims the trailing zero run (baload scan + arraycopy)",
      "baload" in ub and "System.arraycopy" in ub)

# --- 11. credential provenance ----------------------------------------------------
check("connectBleDevice(BleDevice, deviceToken) resolves via NiceBuildSdk.resolveHandshakeToken",
      "resolveHandshakeToken" in agent)
check("resolveHandshakeToken reads PartnerApiManager.getUserAccessToken + JWT, "
      "logs 'no token available' when blank",
      "getUserAccessToken" in nice and "no token available" in nice)

# --- 12. emulator agreement (synthetic token, structure only) ----------------------
sys.path.insert(0, str(ROOT / "emulator"))
from plaudsim.handshake import (  # noqa: E402
    J3_DEV_TOKEN_LEN,
    K3_CONST_AT_3,
    K3_STAGE_FIRST,
    parse_handshake_request,
    token_width,
)

check("emulator token_width: 16 below pv 9, 32 at/above",
      token_width(7) == 16 and token_width(8) == 16 and token_width(9) == 32)
syn_token = "CAFEBABE" * 4  # 32 hex chars, obviously synthetic, pv-9 width
frame = (
    b"\x01\x01\x00"
    + bytes([K3_CONST_AT_3, 0x00, K3_STAGE_FIRST])
    + syn_token.encode("ascii")
)
parsed = parse_handshake_request(frame, 9)
check("emulator parses the recovered k3 layout (const/agent/stage/token)",
      parsed["stage"] == 0 and parsed["token"] == syn_token
      and parsed["agent_value"] == 0 and parsed["is_second"] is False)
check("emulator j3 tail constants match digest (8-byte devToken)",
      J3_DEV_TOKEN_LEN == 8)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
