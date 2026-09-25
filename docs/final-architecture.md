# Final architecture — Plaud / TinnoTech BLE recorder reconstruction

Four layers, each labelled with the strongest evidence class actually
achieved. Classes never collapse: SDK_PROVEN, BYTECODE_PROVEN, RUNTIME_PROVEN,
EMULATOR_INTEGRATION_PROVEN, CLOUD_OBSERVED, DEVICE_OBSERVED, INFERRED,
HARNESS_POLICY, UNKNOWN. **DEVICE_OBSERVED occurs nowhere: no Plaud hardware
was involved at any point.**

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Cloud / API                                       CLOUD_OBSERVED only     │
│  gen-key (RSA pair, private key to client) · sn-sign · sn-verify ·        │
│  metadata (X-Device-Signature) · /api/{oauth,sdk,files,devices,workflows} │
│  · consumer api.plaud.ai · developer platform.plaud.ai · legacy TntAgent  │
│  50 endpoints inventoried statically; 0 exercised (all need credentials)  │
│  r7/cloud-endpoint-inventory.md                                           │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ userAccessToken → snSignature + RSA keys (pv≥20 only)
┌───────────────▼──────────────────────────────────────────────────────────┐
│ Official SDK (plaud-sdk.aar 041a6f88…, iOS swiftinterface for names)      │
│  lifecycle q.b0: pv<20 → first_handshake(k3)          RUNTIME_PROVEN     │
│                  pv≥20 → RSA pre-handshake → sealed    BYTECODE_PROVEN    │
│  k3 build (recovery path, local token transform)       RUNTIME_PROVEN     │
│  l3 parse (all 10 fields), syncTime, battStatus,       RUNTIME_PROVEN     │
│    getState, CommonSettings(8) READ×2                                     │
│  transfer: gap → restart-from-cursor, 5 s stall →      BYTECODE_PROVEN    │
│    stopSync; no ACK; no NAK                                               │
│  audio: o4 pass-through → AudioExporter (PLAUD.AI      BYTECODE_PROVEN    │
│    512-B magic → ChaCha → OggS sniff); isOggAudio unread                  │
│  facade: bleBind(protVersion,tz) both = l3 tz byte     RUNTIME_PROVEN     │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ GATT 1910 / 2BB1 write (host→dev) / 2BB0 notify|indicate
┌───────────────▼──────────────────────────────────────────────────────────┐
│ BLE / GATT protocol                                                        │
│  UUIDs 1910/2BB0/2BB1/2902/180F/2A19; MTU 255 req    BYTECODE_PROVEN +    │
│    (517 negotiated on AVD)                            RUNTIME_PROVEN      │
│  type-1 [u8 1][u16le op][payload]; type 2/3/5 marker  BYTECODE_PROVEN     │
│    [u8 t][u32 ffffffff][payload]; little-endian; CRC-16/CCITT-FALSE        │
│  opcodes 1,2,3,4,6,8,9,22,26,28,29,30,101,138 +       BYTECODE_PROVEN;    │
│    markers FE10/FE11/FE12/FE20                         1,3,4,8,9 RUNTIME   │
│  sealed (pv≥20): ChaCha20-Poly1305 key=J nonce=K       BYTECODE_PROVEN +   │
│    AAD=L, seq in AEAD, first seq 2, no ACK; AES-GCM     EMULATOR_INTEGRATION│
│    is Wi-Fi-only (opcode-138 bit 3)                    (synthetic J/K/L)   │
└───────────────┬──────────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────────┐
│ Device (firmware behaviour)                        UNKNOWN except…        │
│  everything the emulator "decides" is HARNESS_POLICY (ledger §12):        │
│    token acceptance, DATA size/pacing, settings values, feature bits,     │
│    l3 timezone, HEAD/TAIL/resume values, advertising branch (U18)         │
│  ─ emulator/plaudsim implements the device side of every proven          │
│    exchange above and refuses to claim portVersion ≥ 20                   │
└──────────────────────────────────────────────────────────────────────────┘
```

## Emulator (`emulator/plaudsim/`)

| module | implements | evidence behind it |
|---|---|---|
| `advertising.py` | scan-record parse rules + builder (`u4.a` three branches) | SDK_PROVEN rules; live branch U18 |
| `profile.py` | Bumble GATT peripheral: 1910/2BB0/2BB1/CCCD/180F; opcodes 1,2,3,4,6,8,9,22,26,28,29,30,138; lifecycle DISCONNECTED→…→BOUND; packet/handshake/settings/feature logs | BYTECODE_PROVEN layouts; RUNTIME/INTEGRATION for 1,3,4,8,9 |
| `handshake.py` | k3/j3 parse, **`build_k3` oracle**, l3/x2 encode, marker framing, FE12 reassembly, J/K/L split | BYTECODE_PROVEN; k3 RUNTIME_PROVEN |
| `filesync.py`, `transfer.py` | file list pages (pv-dependent stride), HEAD/DATA/TAIL/EMPTY, resume, delete; device performs no resend/ACK (client restarts from cursor) | BYTECODE_PROVEN; values POLICY |
| `sealed.py` (+ `tests/sealed_support.py`) | ChaCha20-Poly1305 sealed link with independent M/N counters, replay/stale drop, AEAD-failure semantics; pv≥20 test peripheral | BYTECODE_PROVEN semantics; synthetic keys only |
| `audio.py` | Java-layer audio contracts: 512-B PLAUD.AI header, ChaCha decrypt, OggS sniff, g4 page geometry, Opus packet extraction | BYTECODE_PROVEN; no authentic bytes |

What the emulator deliberately does **not** do: claim portVersion ≥ 20 on the
air (it would have to lie about sealing), mint real tokens/keys/signatures,
speak Wi-Fi transfer (recovered in ledger §7, not implemented), or decode Opus.

## Runtime proof surface (official SDK ↔ this emulator, over netsim)

R4-S3/R5-S1: scan → connect → MTU 517 → discovery → CCCD → `first_handshake:
bind_token_empty` (empty token, as predicted).
R7-S12: `recoveryConnectBleDevice` with synthetic id → **k3 byte-exact** →
`old_protocol_ok` → syncTime → battStatus → getState → CommonSettings ×2 →
`bleBind status=0`. Four runs, two falsifiers (empty id writes nothing; l3
tz=5 ⇒ `bleBind protVersion=5`). See `r7/r7-s12-k3-runtime-capture.md`.

## Evidence provenance chain

```
reference/** (33 repos, commit-pinned, immutable; AAR sha256 pinned)
   └─ scripts/build-evidence.sh → build/evidence/{aar,classes,jadx-out,javap}   (javap = ground truth)
        └─ scripts/extract_evidence_digest.py → docs/evidence-digest.json          (mechanical; diffed by tests)
             └─ tests/test_evidence_conformance.py + javap-pinned tests             (emulator vs bytecode, not vs itself)
                  └─ r5-*/r6-* verifiers (12, all pass) · r7 runtime captures (4 runs)
                       └─ docs/protocol-ledger.md · docs/final-uncertainty-matrix.json · docs/final-closure-report.md
```
