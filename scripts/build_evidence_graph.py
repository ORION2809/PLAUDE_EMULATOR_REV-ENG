#!/usr/bin/env python3
"""Build docs/evidence-graph.json from the archived agent findings.

Nodes: sources (repos / official docs / official npm / baseline docs), subsystems,
and claims (one per agent finding). Edges: source -> claim (with evidence class),
claim -> subsystem. Purely mechanical; re-run after editing the findings file.
"""
import json, re, hashlib, pathlib, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
FINDINGS = ROOT / "docs/product-evidence/agent-findings-2026-09-23.json"
OUT = ROOT / "docs/evidence-graph.json"

SOURCE_ROLES = {
    # plaud-org
    "plaud-sdk-public": "OFFICIAL_SOURCE+OFFICIAL_BINARY", "plaud-embedded-skills": "OFFICIAL_DOC",
    "embedded-capacitor": "OFFICIAL_SOURCE", "embedded-react-native": "OFFICIAL_SOURCE", "embedded-flutter": "OFFICIAL_SOURCE",
    "live-agent": "LINEAGE_FORK(+Plaud additions)", "xiaozhi-esp32": "LINEAGE_FORK", "client-sdk-esp32": "LINEAGE_FORK",
    "live-agent-memory": "LINEAGE_FORK", "plaud-memU-server": "LINEAGE_FORK", "plaud-memU-ui": "LINEAGE_FORK",
    "langfuse": "LINEAGE_FORK(unmodified)", "plaud-opik": "LINEAGE_FORK(unmodified)", "goreplay": "LINEAGE_FORK(+deployment delta)",
    "vite-react-template": "UNRELATED", "yt-DeepResearch-Backend": "UNRELATED",
    # third-party
    "riffado": "THIRD_PARTY_CLOUD_CLIENT", "plaud-api": "THIRD_PARTY_CLOUD_CLIENT", "plaud-toolkit": "THIRD_PARTY_CLOUD_CLIENT",
    "applaud-rsteckler": "THIRD_PARTY_CLOUD_CLIENT", "python-plaud-ai": "THIRD_PARTY_CLOUD_CLIENT", "applaud-landoncrabtree": "UNRELATED",
    # upstream
    "bumble": "EMULATION_SUBSTRATE", "livekit-agents": "UPSTREAM_REFERENCE", "livekit-client-sdk-esp32": "UPSTREAM_REFERENCE",
    "mem0": "UPSTREAM_REFERENCE", "opik": "UPSTREAM_REFERENCE", "xiaozhi-esp32-server": "UPSTREAM_REFERENCE",
    "pyroomacoustics": "UPSTREAM_REFERENCE", "kokoro": "UPSTREAM_REFERENCE", "piper": "UPSTREAM_REFERENCE",
    # non-repo official
    "docs.plaud.ai": "OFFICIAL_DOC", "npm:@plaud-ai/mcp": "OFFICIAL_BINARY", "npm:@plaud-ai/cli": "OFFICIAL_BINARY",
    "build/evidence/javap": "OFFICIAL_BINARY(bytecode)", "docs/protocol-ledger.md": "BASELINE(R1-R7)",
}
SRC_PATTERNS = [(re.compile(p, re.I), name) for p, name in [
    (r"build/evidence/javap|ALL\.txt|javap", "build/evidence/javap"),
    # abbreviations used by the docs-ios-sdk agent: shipped swiftinterfaces vs docs pages
    (r"\b(BLE|WIFI|BASIC):\d", "plaud-sdk-public"), (r"\b(IOS|ADV|CHG|STA|AND|ADVA):\d", "docs.plaud.ai"),
    (r"\bFLT:\d", "embedded-flutter"), (r"\bCLOSURE:\d", "docs/protocol-ledger.md"),
    (r"build/docs-plaud-ai|docs\.plaud\.ai|plaud-embedded_|api-reference_|openapi_|plaud-mcp-cli_", "docs.plaud.ai"),
    (r"npm-plaud|@plaud-ai/mcp|plaud-ai-mcp", "npm:@plaud-ai/mcp"), (r"@plaud-ai/cli|plaud-ai-cli", "npm:@plaud-ai/cli"),
    (r"protocol-ledger|final-closure-report|cloud-endpoint-inventory|r7-s12|LEDGER:", "docs/protocol-ledger.md"),
    (r"plaud-sdk-public|PlaudTemplateApp|com/plaud/template|swiftinterface|DeviceManager\.(kt|swift)|SyncManager|RecordingStore|PlaudAPIService|TranscriptionManager|README\.md:", "plaud-sdk-public"),
    (r"plaud-embedded-skills|SKILL\.md|user-token-script", "plaud-embedded-skills"),
    (r"embedded-capacitor|nextjs-demo|PlaudSdkPlugin", "embedded-capacitor"), (r"embedded-react-native|PlaudSdkModule|react-native-demo", "embedded-react-native"),
    (r"embedded-flutter|plaud_sdk\.dart|plugins/plaud_sdk", "embedded-flutter"),
    (r"live-agent-memory|embedchain", "live-agent-memory"), (r"live-agent(?!-memory)|xiaozhi-server|live_agent|custom_config\.yaml", "live-agent"),
    (r"plaud-org/xiaozhi-esp32|custom_wake_word|sdkconfig|Korvo|korvo", "xiaozhi-esp32"), (r"client-sdk-esp32|voice_agent", "client-sdk-esp32"),
    (r"plaud-memU-server|memU-server|memu-py", "plaud-memU-server"), (r"plaud-memU-ui|memU-ui|hippocampus", "plaud-memU-ui"),
    (r"langfuse", "langfuse"), (r"plaud-opik|comet-ml/opik", "plaud-opik"), (r"goreplay|Jenkinsfile", "goreplay"),
    (r"vite-react-template", "vite-react-template"), (r"yt-DeepResearch", "yt-DeepResearch-Backend"),
    (r"riffado", "riffado"), (r"plaud-api(?!-)|src/plaud/", "plaud-api"), (r"plaud-toolkit|packages/core|packages/obsidian|packages/mcp", "plaud-toolkit"),
    (r"applaud-rsteckler|server/src/plaud|server/src/auth|server/src/sync", "applaud-rsteckler"), (r"python-plaud-ai|plaud_ai/", "python-plaud-ai"),
    (r"applaud-landoncrabtree", "applaud-landoncrabtree"), (r"upstream/mem0", "mem0"), (r"upstream/opik", "opik"),
    (r"xiaozhi-esp32-server", "xiaozhi-esp32-server"), (r"livekit-agents", "livekit-agents"), (r"livekit-client-sdk-esp32", "livekit-client-sdk-esp32"), (r"bumble", "bumble"),
]]

def sources_for(text):
    out = []
    for rx, name in SRC_PATTERNS:
        if rx.search(text or "") and name not in out: out.append(name)
    return out

def norm_class(c):
    c = (c or "").upper()
    for k in ("RUNTIME", "PROVEN_BYTECODE", "BYTECODE", "PROVEN_OFFICIAL_SOURCE", "OFFICIAL_DOC", "OFFICIAL_BINARY", "CORROBORATED",
              "CLOUD_OBSERVED", "LINEAGE", "INFERRED", "UNKNOWN", "PROVEN", "CONFIRMED", "HIGH", "MEDIUM", "LOW", "CLAIM", "PLAUSIBLE"):
        if k in c: return {"BYTECODE": "PROVEN_BYTECODE", "PROVEN": "PROVEN_OFFICIAL_SOURCE", "CONFIRMED": "OFFICIAL_DOC",
                           "HIGH": "OFFICIAL_DOC", "MEDIUM": "OFFICIAL_DOC", "LOW": "INFERRED", "CLAIM": "CLAIM", "PLAUSIBLE": "INFERRED"}.get(k, k)
    return "UNCLASSIFIED"

def main():
    data = json.load(open(FINDINGS))
    nodes, edges = [], []
    seen = set()
    def add(nid, ntype, **attrs):
        if nid in seen: return
        seen.add(nid); nodes.append({"id": nid, "type": ntype, **attrs})
    for s, role in SOURCE_ROLES.items(): add("src:" + s, "source", role=role)
    subsystems = set()
    for label, d in data.items():
        add("agent:" + label, "agent", area=d.get("source_cluster") or d.get("area"), role=d.get("evidence_role"))
        for i, f in enumerate(d.get("findings", [])):
            sub = (f.get("subsystem") or "Unknown").split("/")[0].strip().title()
            subsystems.add(sub)
            cid = f"claim:{label}:{i}"
            klass = norm_class(f.get("confidence"))
            add(cid, "claim", subsystem=sub, evidence_class=klass, behavior=f.get("behavior"), evidence=f.get("evidence"),
                notes=f.get("notes"), agent=label)
            edges.append({"from": "agent:" + label, "to": cid, "rel": "asserts"})
            edges.append({"from": cid, "to": "subsystem:" + sub, "rel": "about"})
            for s in sources_for((f.get("evidence") or "") + " " + (f.get("source_repo") or "")):
                edges.append({"from": "src:" + s, "to": cid, "rel": "evidences", "class": klass})
        for i, u in enumerate(d.get("unexplained_behaviors", [])):
            uid = f"unknown:{label}:{i}"; add(uid, "unknown", text=u, agent=label)
            edges.append({"from": "agent:" + label, "to": uid, "rel": "flags"})
        for i, c in enumerate(d.get("cross_source_disagreements", []) + d.get("doc_vs_code_contradictions", [])):
            cid = f"contradiction:{label}:{i}"; add(cid, "contradiction", text=c, agent=label)
            edges.append({"from": "agent:" + label, "to": cid, "rel": "records"})
        for i, v in enumerate(d.get("dump_claims_verdicts", [])):
            vid = f"verdict:{label}:{i}"; add(vid, "verdict", claim=v.get("claim"), verdict=v.get("verdict"), evidence=v.get("evidence"), agent=label)
            edges.append({"from": "agent:" + label, "to": vid, "rel": "judges"})
    for s in sorted(subsystems): add("subsystem:" + s, "subsystem")
    counts = {}
    for n in nodes: counts[n["type"]] = counts.get(n["type"], 0) + 1
    graph = {"_comment": "Mechanically built by scripts/build_evidence_graph.py from docs/product-evidence/agent-findings-2026-09-23.json (16 extraction agents, 2026-09-23). Every claim carries the agent that asserted it, its evidence citation and normalised evidence class; source->claim edges are derived from citation text. Not hand-edited.",
             "generated": datetime.datetime.utcnow().isoformat() + "Z",
             "findings_sha256": hashlib.sha256(FINDINGS.read_bytes()).hexdigest(),
             "counts": counts, "nodes": nodes, "edges": edges}
    OUT.write_text(json.dumps(graph, indent=1) + "\n")
    print("wrote", OUT, counts, "edges:", len(edges))

if __name__ == "__main__":
    main()
