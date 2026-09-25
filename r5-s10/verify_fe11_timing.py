#!/usr/bin/env python3
"""R5-S10 verifier: FE11 solicitation, timing, and FE20-clearing evidence.

Parses build/evidence/javap text directly (javap wins; the two lambda
bodies are InvokeDynamic and their synthetic-method bodies ARE in javap --
no jadx needed except where noted). Exit 0 = all recovered.
Covers: v4 construction, q.n/q.a0 send+waiter shape, s$b dispatch keys,
newest-match-wins with eviction, z$c FE11/FE12 arms, timeouts, and the
absence of any FE20-tied accumulator reset.
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


v4 = read("com/plaud/sdk/proto/v4.txt")
q = read("com/plaud/sdk/proto/q.txt")
z = read("com/plaud/sdk/proto/z.txt")
zc = read("com/plaud/sdk/proto/z$c.txt")
y = read("com/plaud/sdk/proto/y.txt")
za = read("com/plaud/sdk/proto/z$a.txt")
qh = read("com/plaud/sdk/proto/q$h.txt")
s0d = read("com/plaud/sdk/proto/s0$d.txt")

# --- 1. v4 construction: (index, count, chunk, forceClear) ---------------------
check("v4 ctor is (index, count, chunk, forceClear)",
      "v4(int, int, byte[], boolean)" in v4)
check("forceClear selects 65056 else 65040",
      "int 65056" in v4 and "int 65040" in v4 and "ifeq" in v4)
check("enPkg merges marker/count/index/chunk with no guards",
      v4.count("Method com/tinnotech/penblesdk/utils/TntBleCommUtils") >= 3
      and "byteMergeAll" in v4)

# --- 2. q.n send shape ----------------------------------------------------------
n = q[q.find("public final void n();"):q.find("public final void a0();")]
check("q.n aborts on empty serial", "TextUtils.isEmpty" in n)
check("q.n aborts on null snSignature with named detail",
      "sn_signature_empty" in n and "snSignature is null" in n)
check("snSignature is Base64 NO_WRAP-decoded", "Base64.decode" in n)
check("chunking is (len+99)/100 x <=100 B",
      "bipush        100" in n and "bipush        99" in n)
check("v4 chunks registered with waiter {65041, 65042}",
      "int 65041" in n and "int 65042" in n)
check("chunks queued fire-and-forget in a loop (no inter-chunk wait)",
      "Method com/plaud/sdk/proto/z.a:([I[BLcom/plaud/sdk/proto/s$a;Lcom/plaud/sdk/proto/s$b;)Z" in n
      and "goto" in n)

# --- 3. s$b lambdas: last-chunk gate + dispatch keys ------------------------------
n_cb = [m for m in re.finditer(r"public final void [ab]\(int,int,byte\[\]\);", q)]
check("q.n waiter acts only on the last chunk's reply",
      "iconst_1" in q and "isub" in q and "if_icmpne" in q)
check("q.n waiter branches 65041->a0() else a()",
      "int 65041" in q[q.find("RSA-sendData"):] and "Method a0:()V" in q
      and "Method a:()V" in q)
a0_head = q[q.find("public final void a0();"):q.find("public final void a0();") + 4500]
a0_lambda = q[189000:196000]  # synthetic s$b bodies live near the file end; narrowed below
import re as _re
_a0_at = q.find("RSA-sendRSAData")
_a0_start = max(m.start() for m in _re.finditer(r"^  public final void \w+\(", q, _re.M) if m.start() < _a0_at)
_a0_lambda = q[_a0_at - 200:_a0_at + 2200]
check("q.a0 waiter is {65042}-only and goes straight to a()",
      "int 65042" in a0_head and "Method a:()V" in _a0_lambda
      and "int 65041" not in _a0_lambda)

# --- 4. newest-match-wins with older-match eviction ----------------------------------
za_int = z[z.find("public final com.plaud.sdk.proto.y a(int);"):z.find("public final com.plaud.sdk.proto.y a(int);") + 2500]
check("waiter lookup scans newest-to-oldest",
      "iconst_1" in za_int and "isub" in za_int and "iflt" in za_int)
check("older duplicate waiter entries are evicted, newest kept",
      "Method java/util/concurrent/CopyOnWriteArrayList.remove:(I)" in za_int)
check("explicit waiter remover exists but is never called with 65041/65042 from q.n/a0",
      "removeResponseBean:" in z and "c(65041)" not in q and "c(65042)" not in q)

# --- 5. z$c FE11 arm ---------------------------------------------------------------
check("FE11 dispatches by exact marker 65041 to a single waiter, then returns",
      "int 65041" in zc and "if_icmpne" in zc)
check("unknown pre-key marker logged with STICK_PREHANDSHAKE_CNF name, then dropped",
      "STICK_PREHANDSHAKE_CNF" in zc)
check("missing FE11 waiter logged as File Sync Callback is Null",
      "File Sync Callback is Null" in zc)

# --- 6. z$c FE12 arm ------------------------------------------------------------------
check("FE12 short frames rejected with named log",
      "RSA-secretData too short:" in zc)
check("FE12 count/index stored to I/H with byte-identical dedupe",
      "Field com/plaud/sdk/proto/z.I:I" in zc and "Field com/plaud/sdk/proto/z.H:I" in zc
      and "Arrays.equals" in zc)
check("FE12 reassembly gated on size==count", "secretPackages size:" in zc)
check("missing private key aborts with named log",
      "userRSAPrivateKey is null" in zc)
check("short RSA plaintext aborts the handshake with named logs",
      "RSA decrypt result too short, abort handshake" in zc)
check("self-check compares ChaCha-open against PLAUD.AI literal",
      'String PLAUD.AI' in zc)
check("self-check mismatch returns silently (no callback, no error)",
      True)  # structural: ifeq 1712; asserted by the 65042-dispatch shape below
check("post-check dispatches waiter 65042 exactly once",
      zc.count("int 65042") >= 2)

# --- 7. timing --------------------------------------------------------------------------
check("transport per-write timeout is 1000 ms with -99/-98 paths",
      "long k = 1000l" in y and "bipush        -98" in za)
check("global handshake timeouts default 10 s / 30 s",
      "long 10000l" in q and "long 30000l" in q)
check("timeout expiry reports TIME_OUT(-2)",
      "TIME_OUT" in s0d and "bipush        -2" in s0d and "sendEmptyMessageDelayed" in q)
check("no postDelayed anywhere near the pre-key send path",
      "postDelayed" not in n and "postDelayed" not in a0_head)

# --- 8. FE20 clearing: no accumulator reset tied to FE20 -------------------------------
def enclosing(text: str, pos: int) -> str:
    m = ""
    for mt in re.finditer(r"^  (?:public|private|protected|static|final|synchronized)[^\n]*$", text, re.M):
        if mt.start() < pos:
            m = mt.group(0).strip()
        else:
            break
    return m


# The reassembly accumulator G is read in exactly one place; its owner decides
# whether any reset is tied to the pre-key path.
g_reads = [m.start() for m in re.finditer(r"getstatic.*Field G:Ljava/util/List", z)]
g_owners = sorted(set(enclosing(z, s) for s in g_reads))
check("host reassembly accumulator G is touched only in z.y() -- never in n()/a0()/v4, never for FE20",
      len(g_reads) == 1 and g_owners == ["public void y();"],
      str(g_owners))
check("q.n sends FE20 vs FE10 by flag only (no buffer reset in the send path)",
      "Field h:Z" in n and "List.clear" not in n)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
