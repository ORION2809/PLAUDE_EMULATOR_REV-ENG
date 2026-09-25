#!/usr/bin/env python3
"""R5-S3 mechanical audit of the SDK second-handshake (j3) stage.

Companion to r5-s2/verify_first_handshake.py (which owns k3/j3 frame
construction): this script owns the d0 ENTRY chain, e/f PROVENANCE, the
shared response DISPATCHER, and the l3-success → syncTime TRANSITION.
Parses build/evidence/javap text directly; exit 0 = recovered.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVAP = ROOT / "build/evidence/javap"
REF = ROOT / "reference/plaud-org/plaud-sdk-public/android/app/src/main/java/com/plaud/template/managers/DeviceManager.kt"
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
j3 = read("com/plaud/sdk/proto/j3.txt")
agent = read("sdk/PlaudDeviceAgent.txt")
tnt = read("com/tinnotech/penblesdk/utils/TntBleCommUtils.txt")

# --- 1. d0 runs if and only if the x2 branch runs ------------------------------
calls = [m.start() for m in re.finditer(r"Method d0", q)]
check("q.d0 has exactly one caller (the x2 response branch)", len(calls) == 1,
      f"{len(calls)} Method-d0 references")
d0 = method_body(q, r"public final void d0\(\);")
check("d0 entry logs twoHandshake/two_handshake", "twoHandshake" in d0 and "two_handshake" in d0)
check("d0 gated on r3.a().e(): false takes the silent return (ifeq 144)",
      "Method com/plaud/sdk/proto/r3.a:()Lcom/plaud/sdk/proto/r3;" in d0
      and "Method com/plaud/sdk/proto/r3.e:()Z" in d0
      and re.search(r"20: ifeq +144", d0) is not None)
w0 = d0.find("newarray       int")
w1 = d0.find("iastore", w0) + 7
window = d0[w0:w1] if 0 < w0 < w1 else ""
check("d0 registers response waiter {1} only (not {1,2})",
      "iconst_1" in window and "iconst_2" not in window)
check("d0 constructs j3 with stage constant 1 (k3 base + second)",
      re.search(r"73: iload_1\n\s+74: iconst_1\n", d0) is not None)
check("d0 passes q.i/q.j as the j3 tail strings",
      "Field i:Ljava/lang/String;" in d0 and "Field j:Ljava/lang/String;" in d0)
check("d0 send-fail path: HandShakeReq:two--sendFail + send_fail + s0$d.g",
      "HandShakeReq:two--sendFail" in d0 and "send_fail" in d0)

# --- 2. e/f provenance: t3.a 7-arg directs params into g/i/j -------------------
a7 = method_body(
    q,
    r"public void a\(com\.tinnotech\.penblesdk\.entity\.BleDevice, java\.lang\.String, "
    r"java\.lang\.String, java\.lang\.String, long, long, boolean\);",
)
gi = a7.find("putfield      #260")
hi = a7.find("putfield      #262")
ii = a7.find("putfield      #264")
ji = a7.find("putfield      #266")
ti = a7.find("putfield      #268")
check("t3.a stores g,h,i,j,t in one ordered block",
      0 < gi < hi < ii < ji < ti)
check("q.g := 2nd String param (aload_2 is the live value at putfield g)",
      "aload_2" in a7[max(0, gi - 800):gi])
seg_i = a7[max(0, ii - 600):ii]
seg_j = a7[max(0, ji - 600):ji]
check("q.i := 3rd String param (token-adjacent deviceToken slot)",
      "aload_3" in seg_i)
check("q.j := 4th String param", "aload         4" in seg_j)
check("5-arg t3.a delegates to the 7-arg form (single assignment site)",
      "Method a:(Lcom/tinnotech/penblesdk/entity/BleDevice;Ljava/lang/String;"
      "Ljava/lang/String;Ljava/lang/String;JJZ)V" in q)

# --- 3. shipped callers leave e/f empty -----------------------------------------
dflt = method_body(agent, r"connectBleDevice\$default")
check("template path: connectBleDevice$default forces deviceToken to empty",
      "Method connectBleDevice:(Lcom/tinnotech/penblesdk/entity/BleDevice;Ljava/lang/String;)V" in dflt)
tpl = Path(REF).read_text()
check("template calls single-arg connectBleDevice(bleDevice) (token defaulted)",
      "PlaudDeviceAgent.connectBleDevice(bleDevice)" in tpl)

# --- 4. shared dispatcher q.a(byte[]) -------------------------------------------
qa = method_body(q, r"public final void a\(byte\[\]\);")
check("dispatcher requires byte0 == 1 (silent drop otherwise)",
      "TntBleCommUtils.a:([BI)I" in qa and "if_icmpne" in qa)
check("dispatcher switches on u16@1 with 'handshake data type' log",
      "TntBleCommUtils.b:([BI)I" in qa and "handshake data type" in qa)
check("type 1 -> l3 ('old HandShakeRsp:')", "old HandShakeRsp:" in qa)
check("type 2 -> x2 ('handshake_get_ssn', 'getSsnRsp ssn:')",
      "handshake_get_ssn" in qa and "getSsnRsp ssn:" in qa)
check("other types -> callback_type_error_ + s0$d.f fail",
      "callback_type_error_" in qa)
check("x2 branch stores ssn via b() then invokes d0() unconditionally",
      re.search(r"Method b:\(Ljava/lang/String;\)V.*?Method d0:\(\)V", qa, re.S) is not None)
check("x2 parse exception -> GetSsnRsp:fail + s0$d.f",
      "GetSsnRsp:fail" in qa)

# --- 5. checkSn does not gate d0 --------------------------------------------------
bs = method_body(q, r"public final void b\(java\.lang\.String\);")
check("checkSn compares advertised serial via r3.a(...)",
      "BleDevice.getSerialNumber" in bs and "checkSn result: Pass" in bs)
check("checkSn Fail posts s0$d.d but returns (d0 still runs after)",
      "checkSn result: Fail" in bs and bs.rstrip().endswith("89: return"))

# --- 6. l3 success -> syncTime; failure -> status ---------------------------------
check("l3 status!=0 -> 'status_' + s0$d.a(status) fail",
      "status_" in qa and "Method com/plaud/sdk/proto/s0$d.a:(I)" in qa)
for lit in ("old_protocol_ok", "sync_time", "getBatLevelCount"):
    check(f"l3-success tail reaches '{lit}'", lit in q)
m7m = method_body(q, r"public void m\(com\.plaud\.sdk\.proto\.c\$b")
check("c0/sync_time path builds the m7 request", "class com/plaud/sdk/proto/m7" in q)
check("battery path K() requests opcode 9",
      re.search(r"bipush +9", method_body(q, r"public void K\(com\.plaud\.sdk\.proto\.c\$b")) is not None)
check("post-l3 fan-out builds getState y2 (opcode 3)",
      "class com/plaud/sdk/proto/y2" in q)
check("post-l3 fan-out builds d4 (opcode 16)",
      "class com/plaud/sdk/proto/d4" in q)

# --- 7. u8 length allocator ---------------------------------------------------------
ci = method_body(tnt, r"public byte\[\] c\(int\);")
check("c(int) allocates exactly 1 byte (u8 length prefix)",
      "iconst_1" in ci and "newarray       byte" in ci)

# --- 8. emulator agreement on a synthetic j3 frame ------------------------------------
sys.path.insert(0, str(ROOT / "emulator"))
from plaudsim.handshake import parse_handshake_request  # noqa: E402

syn_e, syn_f = "DEV123", "ab"  # obviously synthetic tail values
k3base = b"\x01\x01\x00" + bytes([0x02, 0x00, 0x01]) + b"0" * 32
frame = k3base + syn_e.encode() + b"\x00" * (8 - len(syn_e)) + bytes([len(syn_f)]) + syn_f.encode()
p = parse_handshake_request(frame, 9)
check("emulator parses the j3 layout (stage=1, tail split)",
      p["is_second"] is True and p["stage"] == 1 and p["token"] == "0" * 32)
check("emulator recovers synthetic dev_token/user_name",
      p["dev_token"] == syn_e and p["user_name"] == syn_f)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
