# R4-S4: credential provenance (EXPERIMENTAL, read-only investigation)

Verdict: **LEGITIMATE CREDENTIAL UNAVAILABLE — stopped after Phase 1.**
No Phase 2 execution, no handshake emulation, nothing fabricated.

## Credential chain (source-traced)

```text
local.properties: PLAUD_USER_ACCESS_TOKEN      (build-time, absent in repo)
  or Settings → Replace token                    (runtime override, SharedPreferences)
    ↓ RecordingStore.activeUserAccessToken
      → PlaudDeviceAgent.initSDK(context, userAccessToken, customDomain)
        → Partner API gen-key  (RSA pair; 10s wait in DeviceManager.connect,
           "RSA key pair not ready" warning observed live in R4-S3)
        → Partner API sn-sign  (SN signature; observed live failure:
           "Cannot sign device SN - user access token not set")
        → handshake token derivation (observed live: "no token available")
          → q.a() first_handshake → bind_token_empty → disconnect
```

## Why the repository cannot supply one

- `ios/PartnerConfig.xcconfig`: placeholders (`YOUR_USER_ACCESS_TOKEN_HERE`).
- Android `local.properties`: absent (build succeeds with empty token by design).
- No developer/test credential mechanism: no sandbox endpoint, no demo
  token, no mock auth path in template or SDK (only unrelated "sandbox"
  filesystem reference found).
- The token is a real-user JWT (`sub` → userId); minting or borrowing one
  would violate the sprint's credential safety boundary.
- Creating a fresh Plaud developer account is outside this sprint's scope
  (and the handshake additionally needs a device-bound SN signature).

## Last successful / first blocked operation (R4-S3 evidence, unchanged)

Last success: 2BB0 subscribe + CCCD indication write received by Bumble.
First block: `first_handshake detail=bind_token_empty` → UUID_IS_EMPTY(-3).

## What would unblock Phase 2

A legitimate partner `userAccessToken` entered via the template's own
Settings → Replace mechanism (no rebuild, no SDK change), then re-run the
R4-S3 topology and capture the SDK-generated k3 bytes for comparison
against the ledger §5.5 structure.
