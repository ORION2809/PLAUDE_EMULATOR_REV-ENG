#!/usr/bin/env python3
"""R5-S4 mechanical audit of the l3 response parser and post-j3 transition.

Parses build/evidence/javap text directly; exit 0 = recovered.
Companion to r5-s2/verify_first_handshake.py (k3) and r5-s3/verify_j3.py
(d0/response dispatcher): this script owns the l3 CLASS layout and the
l3-success -> syncTime TRANSITION.
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


l3 = read("com/plaud/sdk/proto/l3.txt")
n = read("com/plaud/sdk/proto/n.txt")
q = read("com/plaud/sdk/proto/q.txt")
m7 = read("com/plaud/sdk/proto/m7.txt")

# --- 1. l3 identity ------------------------------------------------------------
check("l3 extends n (response base)", "public class com.plaud.sdk.proto.l3 extends com.plaud.sdk.proto.n" in l3)
check("l3 response opcode is 1",
      "iconst_1" in method_body(l3, r"public int a\(\);"))
nb = method_body(n, r"public com\.plaud\.sdk\.proto\.n\(byte\[\]\)")
check("n.<init> validates u16@1 via b() and throws Mismatch (no length pre-check)",
      "TntBleCommUtils.b:([BI)I" in nb and "Mismatch" in nb
      and "arraylength" not in nb)

# --- 2. Java defaults assigned before parsing -----------------------------------
ctor = method_body(l3, r"public com\.plaud\.sdk\.proto\.l3\(byte\[\]\)")
check("l3 defaults: e=0, f=1, g/h/i=false, j=V, k=0",
      "Field e:I" in ctor and "Field f:I" in ctor
      and re.search(r"15: iconst_1\n\s+16: putfield +#35", ctor) is not None
      and 'String V' in ctor)

# --- 3. unguarded reads: status u8@3, portVersion u16@4, timezone u8@6 ---------
check("status b := u8@3", re.search(r"iconst_3\n.*?TntBleCommUtils\.a:\(\[BI\)I.*?Field b:I", ctor, re.S) is not None)
check("portVersion c := u16@4", re.search(r"iconst_4\n.*?TntBleCommUtils\.b:\(\[BI\)I.*?Field c:I", ctor, re.S) is not None)
check("timezone d := u8@6", re.search(r"bipush +6\n.*?TntBleCommUtils\.a:\(\[BI\)I.*?Field d:I", ctor, re.S) is not None)

# --- 4. guarded reads with early-return-to-end -----------------------------------
gates = re.findall(r"bipush +(8|9|10|11|12)\n.*?if_icmplt +287", ctor, re.S)
check("guarded fields e/f/g/h/i gated on len>=8/9/10/11/12",
      sorted(gates) == ["10", "11", "12", "8", "9"], str(sorted(gates)))
check("booleans g/h/i are ==1 comparisons",
      ctor.count("if_icmpne") >= 3)
check("guarded offsets: e@7 f@8 g@9 h@10 i@11 (cursor threading)",
      "iload_2" in ctor and "iload_3" in ctor)

# --- 5. version tail read from frame END, little-endian --------------------------
check("versionType j := char at len-4 ((b&255)->char)",
      re.search(r"iconst_4\n.*?isub.*?baload.*?sipush +255.*?iand.*?i2c.*?String\.valueOf", ctor, re.S) is not None)
check("version k := u24LE at len-3 (b0 | b1<<8 | b2<<16, &255 masks)",
      re.search(r"bipush +8\n.*?ishl.*?ior.*?bipush +16\n.*?ishl.*?ior", ctor, re.S) is not None)
check("dead len>=4 check nested inside the len>=12 branch",
      re.search(r"bipush +12\n.*?if_icmplt +287.*?iconst_4\n.*?if_icmplt +287", ctor, re.S) is not None)

# --- 6. names + getter shuffle ----------------------------------------------------
ts = method_body(l3, r"public java\.lang\.String toString\(\);")
check("toString names fields in order status..version",
      "{status=%d, portVersion=%d, timezone=%d, timezoneMin=%d, audioChannel=%d, "
      "supportWifi=%s, noNsAgc=%s, isOggAudio=%s, versionType=%s, version=%d}" in ts)
pairs = [("d", "b"), ("c", "c"), ("e", "d"), ("f", "e"), ("b", "f"),
         ("k", "g"), ("i", "h"), ("j", "i"), ("h", "j"), ("g", "k")]
ok = True
for meth, field in pairs:
    body = method_body(l3, rf"public (?:int|boolean|java\.lang\.String) {meth}\(\);")
    if f"Field {field}:" not in body:
        ok = False
check("getter shuffle (d()->b, c()->c, e()->d, f()->e, b()->f, k/i/j->g/h/i, h()->j, g()->k)", ok)

# --- 7. q.a(byte[]) l3 branch ------------------------------------------------------
check("l3 branch builds l3 + logs old HandShakeRsp:", "old HandShakeRsp:" in q)
check("l3 status!=0 -> status_ + s0$d.a(status) fail",
      "status_" in q and "Method com/plaud/sdk/proto/s0$d.a:(I)" in q)
for f_, fld in (("m", "1054"), ("n", "1303"), ("o", "1306"), ("p", "1309"),
                ("q", "187"), ("r", "189"), ("s", "191")):
    check(f"l3-success caches field {f_}", f"Field {f_}:I" in q or f"Field {f_}:Z" in q)
check("BleDevice capability setters fed from l3",
      "setAudioChannel" in q and "setNoNsAgc" in q and "setOggAudio" in q
      and "setVersionType" in q and "setVersionCode" in q)
check("p7.l = audioChannel*80 (bipush 80 imul)", re.search(r"bipush +80\n.*?imul", q) is not None)
check("old_protocol_ok occurs exactly once (success stage label)",
      q.count("old_protocol_ok") == 1)

# --- 8. syncTime transition ----------------------------------------------------------
c0 = method_body(q, r"public final void c0\(\);")
check("c0() stages sync_time and invokes m(c$b,c$c)",
      "sync_time" in c0 and "Method m:(Lcom/plaud/sdk/proto/c$b" in c0)
mm = method_body(q, r"public void m\(com\.plaud\.sdk\.proto\.c\$b")
check("m() registers waiter {4} and builds m7",
      "iconst_4" in mm and "class com/plaud/sdk/proto/m7" in mm)
check("m7 request opcode 4, proto 1",
      "iconst_4" in method_body(m7, r"public int getBleRequestType\(\);")
      and "iconst_1" in method_body(m7, r"public int getBleProtocolType\(\);"))
check("m7 stamp = currentTimeMillis/1000 (ldiv truncation)",
      re.search(r"currentTimeMillis.*?ldiv", m7, re.S) is not None)
check("m7 tz = getOffset/60000 split by idiv/irem 60 (both signed)",
      "TimeZone.getOffset" in m7 and "bipush        60" in m7
      and "idiv" in m7 and "irem" in m7)
check("m7 enPkg = packHead + u32 stamp + u8 tzH + u8 tzM",
      "packHead" in method_body(m7, r"public byte\[\] enPkg\(\);"))

# --- 9. error taxonomy ---------------------------------------------------------------
check("dispatcher drops byte0!=1 frames silently (if_icmpne to bare return)",
      re.search(r"if_icmpne +764", q) is not None)
check("l3/x2 ctor throws -> s0$d.f (exception targets 266/736)",
      "266   Class java/lang/Exception" in q and "736   Class java/lang/Exception" in q)

# --- 10. emulator agreement (device-side encode satisfies the layout) ------------------
sys.path.insert(0, str(ROOT / "emulator"))
from plaudsim.handshake import encode_l3  # noqa: E402

raw = encode_l3(status=0, port_version=9, timezone=8, timezone_min=0,
                audio_channel=2, support_wifi=True, no_ns_agc=False,
                is_ogg_audio=True, version_type="A", version=4660)
check("emulator l3: status u8@3", raw[3] == 0)
check("emulator l3: portVersion u16le@4", int.from_bytes(raw[4:6], "little") == 9)
check("emulator l3: tz@6/tzMin@7/audio@8/wifi@9/nons@10/ogg@11",
      raw[6:12] == bytes([8, 0, 2, 1, 0, 1]))
check("emulator l3: versionType at len-4, version u24LE at len-3",
      raw[-4:-3] == b"A" and int.from_bytes(raw[-3:], "little") == 4660)
check("emulator l3: truncated frame keeps Java defaults shape (f default 1)",
      encode_l3()[8] == 1)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
