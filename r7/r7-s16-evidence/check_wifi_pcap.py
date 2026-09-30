#!/usr/bin/env python3
"""R7-S16 evidence checker: did the pen's WebSocket really cross the emulated Wi-Fi netdev?

Standard library only (run it with PYTHONDONTWRITEBYTECODE=1, like every script here).

Inputs (all but --wifi-pcap are optional; a check whose input is missing is "skipped"):

  --wifi-pcap     pcap written by QEMU `filter-dump` on the netdev `virtio-wifi`, i.e. the
                  Ethernet frames between the emulator's Wi-Fi forwarder and its user-mode
                  (slirp) network, after the emulated access point has removed WPA2.
  --capture-json  the pen's capture (`r7/wifi_capture_device.py`, WIFICAP_CAPTURE).
  --eth-pcap      pcap of the Ethernet netdev `mynet` (emulator `-tcpdump`): negative control.
  --proc-net-tcp  rows of the guest's /proc/net/tcp and /proc/net/tcp6 polled during the run.
  --logcat        the run's logcat (for the SDK's "Device connected: <addr>" line).
  --wifi-status   `cmd wifi status` taken while the phone should be on --ssid (control liveness).

What it checks (transfer mode, the default):

  ws_stream_found         at least one TCP connection to --port carries an HTTP 101 upgrade
  ws_endpoints            every upgraded connection runs --expect-client-ip -> --server-ip:--port
  tcp_reassembly_clean    no gap (inside a stream, or at its end: bytes the peer ACKed that the
                          capture lacks), conflicting overlap, truncated packet or pcap file, and
                          no partial WebSocket frame or unfinished message left at a stream's end
  ws_protocol             RFC 6455 framing rules (client frames masked, server frames not, no
                          stray reserved bits, no reserved opcodes 3-7 or 0xB-0xF, well-formed
                          fragmentation and control frames, valid close codes)
  pdu_out_match           client->server data messages == capture JSON "out" hex, in order
  pdu_in_match            server->client data messages == capture JSON "in" hex, in order
  file_sha256             at least one FileSyncContent transfer finished, and every FINISHED one
                          re-hashes to the expected sha256 (--expected-sha256, else capture JSON
                          stats.file_sha256); unfinished (aborted, restarted) ones are reported only
  eth_no_port             the Ethernet-netdev pcap has no TCP/UDP packet to or from --port
  proc_net_tcp_peer       a guest row  --server-ip:--port <-> --expect-client-ip  ESTABLISHED
  logcat_device_connected every "Device connected: X" line has X == --expect-client-ip
  egress_none             (only with --expect-no-egress) no guest-initiated TCP connection was
                          answered with SYN-ACK, no guest UDP flow (other than DHCP) was answered,
                          and no guest ICMP/ICMPv6 echo request to a host outside --slirp-hosts
                          got an echo reply
A failed comparison or hash becomes "inconclusive" instead of "fail" when the capture itself is
defective (tcp_reassembly_clean not clean).

With --expect-none (the FWD=0 control run) the WebSocket checks invert: no payload byte on
--port in the Wi-Fi pcap, no data entry in the capture JSON, no ESTABLISHED --port row, no
"Device connected" line. Absence alone proves nothing, so control_liveness must also pass: the
Wi-Fi pcap is complete and carries guest traffic, the guest LISTENed on --port, the SDK logged
"Starting WebSocket server on port <port>", the pen logged dial attempts (connect_retry or
connect_failed), and (with --wifi-status) the phone was on --ssid; a missing input or sign makes it
"inconclusive". In both modes a negative check on an empty or truncated pcap is "inconclusive".
With --summary only the pcaps are parsed and summarised.

Every flow that is not the --port stream is listed under "other_flows" (the egress record).

Exit codes: 0 every applicable check passed; 1 at least one check failed; 2 usage, input or
internal error (missing/unreadable file, unsupported pcap format, malformed JSON, or a checker
bug; a report with verdict "input_error"/"internal_error" is still written); 3 inconclusive (no
check failed, but at least one could not be decided: TCP gap, truncated capture, sealed frames,
missing liveness evidence).

Capture-JSON assumptions (from r7/wifi_capture_device.py and the R7-S15 run-2 capture):
  * top-level "wifi" is a list; entries whose "dir" is "out" (pen -> phone, i.e. WebSocket
    client -> server) or "in" (phone -> pen) carry "hex": the complete payload of ONE binary
    WebSocket message as the pen sent or received it (the PDU, or the sealed PDU when keyed);
    other "dir" values (event, state) are ignored;
  * within each direction the list order is wire order; "t" is seconds since stats.t0 (host
    clock), used only for an informational clock-offset figure;
  * entries are not tagged by Wi-Fi device: when several devices ran, the per-direction
    sequences are compared as one concatenation, in order;
  * stats.file_sha256 is the served file's sha256.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import ipaddress
import json
import math
import re
import struct
import sys
import traceback
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_INCONCLUSIVE = 0, 1, 2, 3

TCP_FIN, TCP_SYN, TCP_RST, TCP_PSH, TCP_ACK = 0x01, 0x02, 0x04, 0x08, 0x10
MASK32 = 0xFFFFFFFF

#: Must equal plaudsim.wifi.MESSAGE_NAMES (tests/test_r7_s16_pcap.py checks it).
MESSAGE_NAMES = {
    0: "UniversalErr", 1: "Handshake", 2: "SayHello", 3: "Heartbeat", 4: "WifiClose", 5: "Tips",
    11: "GetFileList", 12: "FileSync", 13: "FileSyncContent", 14: "FileDelete", 15: "FileSyncStop",
    16: "ExtendExitTime", 20: "SendOTAFileInfo", 21: "RequestOTAPackage", 22: "OTAStatusSync",
    100: "SpeedTest", 101: "GetDeviceLog",
}
MSG_FILE_SYNC_CONTENT = 13
#: WifiAgentImpl treats a frame as sealed when u16le at offset 4 exceeds 200 (plaudsim.wifi).
SEALED_TYPE_THRESHOLD = 200
WS_OPCODES = {0: "continuation", 1: "text", 2: "binary", 8: "close", 9: "ping", 10: "pong"}
ECHO_REQUEST, ECHO_REPLY = (8, 128), (0, 129)     # ICMP and ICMPv6 echo types


class InputError(Exception):
    """A usage or input problem: exit code 2."""


# --------------------------------------------------------------------------- pcap reading


@dataclass
class Packet:
    index: int
    ts: float
    caplen: int
    origlen: int
    data: bytes


@dataclass
class Pcap:
    path: str
    linktype: int
    snaplen: int
    packets: list
    file_truncated: bool


_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1e-6),
    b"\xa1\xb2\xc3\xd4": (">", 1e-6),
    b"\x4d\x3c\xb2\xa1": ("<", 1e-9),
    b"\xa1\xb2\x3c\x4d": (">", 1e-9),
}
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = (101, 228, 229)  # raw IP, raw IPv4, raw IPv6


def read_pcap(path: str) -> Pcap:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read pcap {path}: {exc}") from exc
    if len(raw) < 24:
        raise InputError(f"{path}: {len(raw)} bytes, shorter than a pcap global header")
    magic = raw[:4]
    if magic == b"\x0a\x0d\x0d\x0a":
        raise InputError(f"{path}: pcapng is not supported (classic pcap only)")
    if magic not in _PCAP_MAGICS:
        raise InputError(f"{path}: not a pcap file (magic {magic.hex()})")
    endian, unit = _PCAP_MAGICS[magic]
    _vmaj, _vmin, _tz, _sig, snaplen, linktype = struct.unpack(endian + "HHiIII", raw[4:24])
    linktype &= 0x0FFFFFFF
    if linktype != LINKTYPE_ETHERNET and linktype not in LINKTYPE_RAW:
        raise InputError(f"{path}: unsupported link type {linktype} (Ethernet or raw IP only)")
    packets = []
    off, truncated = 24, False
    while off < len(raw):
        if off + 16 > len(raw):
            truncated = True
            break
        sec, frac, incl, orig = struct.unpack(endian + "IIII", raw[off:off + 16])
        off += 16
        if off + incl > len(raw):
            truncated = True
            break
        packets.append(Packet(len(packets), sec + frac * unit, incl, orig, raw[off:off + incl]))
        off += incl
    return Pcap(path, linktype, snaplen, packets, truncated)


# --------------------------------------------------------------------------- decoding


@dataclass
class Decoded:
    index: int
    ts: float
    l3: str                      # ipv4 | ipv6 | arp | other
    proto: str                   # tcp | udp | icmp | icmpv6 | arp | ip-proto-N | ethertype-0xNNNN
    src: Optional[str] = None
    dst: Optional[str] = None
    sport: Optional[int] = None
    dport: Optional[int] = None
    seq: Optional[int] = None
    ack: Optional[int] = None
    flags: int = 0
    payload: bytes = b""
    fragment: bool = False
    truncated: bool = False
    icmp_type: Optional[int] = None
    length: int = 0


def _decode_l4(d: Decoded, proto: int, l4: bytes, truncated: bool) -> None:
    if proto == 6:
        d.proto = "tcp"
        if len(l4) < 20:
            d.truncated = True
            return
        d.sport, d.dport, d.seq, d.ack = struct.unpack(">HHII", l4[:12])
        doff = (l4[12] >> 4) * 4
        d.flags = l4[13]
        if doff < 20 or doff > len(l4):
            d.truncated = True
            return
        d.payload = bytes(l4[doff:])
    elif proto == 17:
        d.proto = "udp"
        if len(l4) < 8:
            d.truncated = True
            return
        d.sport, d.dport, ulen = struct.unpack(">HHH", l4[:6])
        d.payload = bytes(l4[8:ulen]) if 8 <= ulen <= len(l4) else bytes(l4[8:])
    elif proto == 1:
        d.proto = "icmp"
        d.icmp_type = l4[0] if l4 else None
    elif proto == 58:
        d.proto = "icmpv6"
        d.icmp_type = l4[0] if l4 else None
    else:
        d.proto = f"ip-proto-{proto}"
    d.truncated = d.truncated or truncated


def _decode_ipv4(d: Decoded, b: bytes) -> None:
    d.l3 = "ipv4"
    if len(b) < 20 or b[0] >> 4 != 4:
        d.proto, d.truncated = "ipv4-malformed", True
        return
    ihl = (b[0] & 0x0F) * 4
    total = struct.unpack(">H", b[2:4])[0]
    frag = struct.unpack(">H", b[6:8])[0]
    proto = b[9]
    d.src = str(ipaddress.IPv4Address(b[12:16]))
    d.dst = str(ipaddress.IPv4Address(b[16:20]))
    truncated = total > len(b)
    body = b[ihl:min(total, len(b))]          # IP total length trims Ethernet padding
    d.fragment = bool(frag & 0x2000) or (frag & 0x1FFF) != 0
    if d.fragment and (frag & 0x1FFF) != 0:
        d.proto = {6: "tcp", 17: "udp"}.get(proto, f"ip-proto-{proto}")
        d.truncated = truncated
        return
    _decode_l4(d, proto, body, truncated)


def _decode_ipv6(d: Decoded, b: bytes) -> None:
    d.l3 = "ipv6"
    if len(b) < 40 or b[0] >> 4 != 6:
        d.proto, d.truncated = "ipv6-malformed", True
        return
    plen = struct.unpack(">H", b[4:6])[0]
    nxt = b[6]
    d.src = str(ipaddress.IPv6Address(b[8:24]))
    d.dst = str(ipaddress.IPv6Address(b[24:40]))
    truncated = 40 + plen > len(b)
    body = b[40:min(40 + plen, len(b))]
    for _ in range(8):                         # hop-by-hop, routing, fragment, destination options
        if nxt in (0, 43, 60) and len(body) >= 2:
            hlen = (body[1] + 1) * 8
            nxt, body = body[0], body[hlen:]
        elif nxt == 44 and len(body) >= 8:
            d.fragment = True
            nxt, body = body[0], body[8:]
        else:
            break
    _decode_l4(d, nxt, body, truncated)


def decode(pkt: Packet, linktype: int) -> Decoded:
    d = Decoded(index=pkt.index, ts=pkt.ts, l3="other", proto="other", length=pkt.origlen,
                truncated=pkt.caplen < pkt.origlen)
    b = pkt.data
    if linktype == LINKTYPE_ETHERNET:
        if len(b) < 14:
            d.truncated = True
            return d
        etype, off = struct.unpack(">H", b[12:14])[0], 14
        while etype in (0x8100, 0x88A8) and len(b) >= off + 4:
            etype, off = struct.unpack(">H", b[off + 2:off + 4])[0], off + 4
        body = b[off:]
    else:
        body = b
        etype = 0x0800 if body and body[0] >> 4 == 4 else 0x86DD
    trunc_before = d.truncated
    if etype == 0x0800:
        _decode_ipv4(d, body)
    elif etype == 0x86DD:
        _decode_ipv6(d, body)
    elif etype == 0x0806:
        d.l3 = d.proto = "arp"
        if len(body) >= 28:
            d.src = str(ipaddress.IPv4Address(body[14:18]))
            d.dst = str(ipaddress.IPv4Address(body[24:28]))
            d.icmp_type = struct.unpack(">H", body[6:8])[0]   # ARP op, reused field
    else:
        d.proto = f"ethertype-0x{etype:04x}"
    d.truncated = d.truncated or trunc_before
    return d


# --------------------------------------------------------------------------- TCP reassembly


@dataclass
class HalfStream:
    isn: Optional[int] = None
    segments: list = field(default_factory=list)   # (seq, payload, ts, packet index)
    acks: list = field(default_factory=list)       # ACK numbers the PEER sent for this direction
    syn: int = 0
    fin: bool = False
    rst: bool = False
    packets: int = 0
    truncated: int = 0


@dataclass
class TcpConn:
    client: tuple
    server: tuple
    first_ts: float
    last_ts: float
    c2s: HalfStream = field(default_factory=HalfStream)
    s2c: HalfStream = field(default_factory=HalfStream)
    syn_ack: bool = False


@dataclass
class Assembled:
    data: bytes
    arrivals: list          # sorted (start, end, ts) of the segments that added new bytes
    issues: list
    unassembled_bytes: int
    no_syn: bool


def _tail_check(h: HalfStream, base: Optional[int], captured: int, issues: list) -> None:
    """A gap at the END of a stream has no later segment to reveal it; the peer's ACKs do.
    A FIN takes one sequence number, so an ACK of captured + 1 is normal (the FIN itself may be
    the uncaptured part); anything beyond that is data the peer received and the capture lacks."""
    if base is None or any(i["kind"] == "gap" for i in issues):
        return
    rels = [(a - base) & MASK32 for a in h.acks]
    rels = [r for r in rels if r < 1 << 31]
    if rels and max(rels) > captured + 1:
        issues.append({"kind": "tail_missing", "captured": captured, "peer_acked": max(rels),
                       "missing_at_least": max(rels) - captured - 1})


def assemble(h: HalfStream) -> Assembled:
    issues: list = []
    with_data = [s for s in h.segments if s[1]]
    if not with_data:
        _tail_check(h, (h.isn + 1) & MASK32 if h.isn is not None else None, 0, issues)
        return Assembled(b"", [], issues, 0, h.isn is None)
    if h.isn is not None:
        base = (h.isn + 1) & MASK32
    else:
        base = with_data[0][0]
        issues.append({"kind": "no_syn", "detail": "stream start not captured; offsets relative to the first segment"})
    placed = []
    for seq, payload, ts, idx in with_data:
        rel = (seq - base) & MASK32
        if rel >= 1 << 31:
            issues.append({"kind": "before_base", "packet": idx, "len": len(payload)})
            continue
        placed.append((rel, payload, ts, idx))
    placed.sort(key=lambda s: (s[0], -len(s[1])))
    buf = bytearray()
    arrivals = []
    unassembled = 0
    for n, (rel, payload, ts, idx) in enumerate(placed):
        end = rel + len(payload)
        if rel > len(buf):
            issues.append({"kind": "gap", "at": len(buf), "next_segment_at": rel, "packet": idx})
            unassembled = sum(len(p) for _r, p, _t, _i in placed[n:])
            break
        overlap = min(end, len(buf)) - rel
        if overlap > 0 and bytes(buf[rel:rel + overlap]) != payload[:overlap]:
            issues.append({"kind": "overlap_conflict", "at": rel, "len": overlap, "packet": idx})
        if end > len(buf):
            arrivals.append((len(buf), end, ts))
            buf.extend(payload[len(buf) - rel:])
    _tail_check(h, base, len(buf), issues)
    return Assembled(bytes(buf), arrivals, issues, unassembled, h.isn is None)


def ts_at(arrivals: list, offset: int) -> Optional[float]:
    """Capture time of the segment that delivered stream byte `offset`."""
    starts = [a[0] for a in arrivals]
    i = bisect.bisect_right(starts, offset) - 1
    if 0 <= i < len(arrivals) and arrivals[i][0] <= offset < arrivals[i][1]:
        return arrivals[i][2]
    return None


def collect_port_conns(decoded: list, port: int) -> list:
    """TCP connections with `port` on the server side, in order of first packet."""
    conns: list = []
    latest: dict = {}
    for d in decoded:
        if d.proto != "tcp" or d.sport is None or d.fragment:
            continue
        if d.dport == port:
            key, from_client = (d.src, d.sport, d.dst, d.dport), True
        elif d.sport == port:
            key, from_client = (d.dst, d.dport, d.src, d.sport), False
        else:
            continue
        conn = latest.get(key)
        is_client_syn = from_client and d.flags & TCP_SYN and not d.flags & TCP_ACK
        if conn is not None and is_client_syn and (
            conn.c2s.segments and any(s[1] for s in conn.c2s.segments)
            or conn.c2s.fin or conn.c2s.rst or conn.s2c.rst or conn.s2c.fin
        ):
            conn = None
        if conn is None:
            conn = TcpConn(client=(key[0], key[1]), server=(key[2], key[3]), first_ts=d.ts, last_ts=d.ts)
            latest[key] = conn
            conns.append(conn)
        conn.last_ts = d.ts
        h = conn.c2s if from_client else conn.s2c
        h.packets += 1
        if d.truncated:
            h.truncated += 1
        if d.flags & TCP_SYN:
            h.syn += 1
            h.isn = d.seq
            if not from_client and d.flags & TCP_ACK:
                conn.syn_ack = True
        if d.flags & TCP_FIN:
            h.fin = True
        if d.flags & TCP_RST:
            h.rst = True
        if d.flags & TCP_ACK and d.ack is not None:
            (conn.s2c if from_client else conn.c2s).acks.append(d.ack)
        if d.payload:
            h.segments.append((d.seq, d.payload, d.ts, d.index))
    return conns


# --------------------------------------------------------------------------- HTTP + WebSocket


def split_http(buf: bytes) -> tuple:
    """(start line, {lower-case header: value}, rest) or (None, {}, buf) when no header end."""
    idx = buf.find(b"\r\n\r\n")
    if idx < 0:
        return None, {}, buf
    lines = buf[:idx].decode("latin-1").split("\r\n")
    headers: dict = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            headers[k] = f"{headers[k]}, {v.strip()}" if k in headers else v.strip()
    return lines[0], headers, buf[idx + 4:]


@dataclass
class WsFrame:
    offset: int
    fin: bool
    rsv: int
    opcode: int
    masked: bool
    length: int
    header_len: int
    payload: bytes


def _unmask(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return b""
    n = len(payload)
    keystream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(keystream, "big")).to_bytes(n, "big")


def parse_ws_frames(data: bytes, base_offset: int = 0) -> tuple:
    """Split a byte stream into frames. Returns (frames, trailing partial bytes, violations)."""
    frames, violations = [], []
    i = 0
    while len(data) - i >= 2:
        b0, b1 = data[i], data[i + 1]
        j = i + 2
        length = b1 & 0x7F
        if length == 126:
            if len(data) < j + 2:
                break
            length = struct.unpack(">H", data[j:j + 2])[0]
            j += 2
            if length < 126:
                violations.append({"offset": base_offset + i, "kind": "non-minimal 16-bit length"})
        elif length == 127:
            if len(data) < j + 8:
                break
            length = struct.unpack(">Q", data[j:j + 8])[0]
            j += 8
            if length >> 63:
                violations.append({"offset": base_offset + i, "kind": "64-bit length with top bit set"})
                break
            if length < 65536:
                violations.append({"offset": base_offset + i, "kind": "non-minimal 64-bit length"})
        masked = bool(b1 & 0x80)
        key = b""
        if masked:
            if len(data) < j + 4:
                break
            key = data[j:j + 4]
            j += 4
        if len(data) < j + length:
            break
        raw = data[j:j + length]
        frames.append(WsFrame(base_offset + i, bool(b0 & 0x80), (b0 >> 4) & 7, b0 & 0x0F, masked, length,
                              j - i, _unmask(raw, key) if masked else bytes(raw)))
        i = j + length
    return frames, len(data) - i, violations


@dataclass
class DeflateParams:
    client_no_context_takeover: bool = False
    server_no_context_takeover: bool = False
    client_max_window_bits: int = 15
    server_max_window_bits: int = 15


def parse_deflate(extensions: Optional[str]) -> Optional[DeflateParams]:
    if not extensions:
        return None
    for ext in extensions.split(","):
        parts = [p.strip() for p in ext.split(";")]
        if parts[0].lower() != "permessage-deflate":
            continue
        p = DeflateParams()
        for param in parts[1:]:
            k, _, v = param.partition("=")
            k, v = k.strip().lower(), v.strip().strip('"')
            if k == "client_no_context_takeover":
                p.client_no_context_takeover = True
            elif k == "server_no_context_takeover":
                p.server_no_context_takeover = True
            elif k == "client_max_window_bits" and v:
                p.client_max_window_bits = int(v)
            elif k == "server_max_window_bits" and v:
                p.server_max_window_bits = int(v)
        return p
    return None


@dataclass
class WsMessage:
    direction: str          # out = client -> server (pen -> phone), in = server -> client
    conn: int
    opcode: int
    payload: bytes
    compressed: bool
    frames: int
    end_offset: int
    ts: Optional[float] = None


def valid_close_code(code: int) -> bool:
    """Close codes that may appear on the wire: 1000-1003 and 1007-1011 (RFC 6455 7.4.1), 1012-1014
    (IANA registry), 3000-4999 (libraries, applications). 1004 is reserved; 1005, 1006 and 1015 must
    not be sent; everything else below 3000 is unassigned."""
    return 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999


def assemble_messages(frames: list, direction: str, conn: int, deflate: Optional[DeflateParams]) -> tuple:
    """Frames -> (data messages, control frames, violations, incomplete message or None)."""
    expect_masked = direction == "out"
    messages, controls, violations = [], [], []
    current: Optional[dict] = None
    if deflate is not None:
        no_ctx = deflate.client_no_context_takeover if direction == "out" else deflate.server_no_context_takeover
        wbits = deflate.client_max_window_bits if direction == "out" else deflate.server_max_window_bits
    else:
        no_ctx, wbits = False, 15
    inflater = zlib.decompressobj(-wbits)
    for f in frames:
        if f.masked != expect_masked:
            violations.append({"offset": f.offset, "kind": "client frame not masked" if expect_masked
                               else "server frame masked"})
        allowed_rsv = 4 if (deflate is not None and f.opcode in (1, 2)) else 0
        if f.rsv & ~allowed_rsv:
            violations.append({"offset": f.offset, "kind": f"reserved bits {f.rsv:03b} set"})
        if f.opcode >= 8:
            entry: dict = {"offset": f.offset, "opcode": WS_OPCODES.get(f.opcode, f"op{f.opcode}"), "len": f.length}
            if f.opcode not in (8, 9, 10):     # RFC 6455 5.2: 0xB-0xF are reserved control opcodes
                violations.append({"offset": f.offset, "kind": f"reserved control opcode {f.opcode}"})
            if not f.fin or f.length > 125:
                violations.append({"offset": f.offset, "kind": "fragmented or oversized control frame"})
            if f.opcode == 8 and len(f.payload) == 1:   # 5.5.1: a close body starts with a 2-byte code
                violations.append({"offset": f.offset, "kind": "close frame with a 1-byte body"})
            if f.opcode == 8 and len(f.payload) >= 2:
                code = struct.unpack(">H", f.payload[:2])[0]
                entry["close_code"] = code
                entry["close_reason"] = f.payload[2:].decode("utf-8", "replace")
                if not valid_close_code(code):
                    violations.append({"offset": f.offset, "kind": f"invalid close code {code}"})
                try:
                    f.payload[2:].decode("utf-8")
                except UnicodeDecodeError:
                    violations.append({"offset": f.offset, "kind": "close reason is not UTF-8"})
            controls.append(entry)
            continue
        if f.opcode in (1, 2):
            if current is not None:
                violations.append({"offset": f.offset, "kind": "new data frame inside a fragmented message"})
            current = {"opcode": f.opcode, "rsv1": bool(f.rsv & 4), "parts": [f.payload], "frames": 1}
        elif f.opcode == 0:
            if current is None:
                violations.append({"offset": f.offset, "kind": "continuation frame without a message"})
                continue
            current["parts"].append(f.payload)
            current["frames"] += 1
        else:
            violations.append({"offset": f.offset, "kind": f"unknown opcode {f.opcode}"})
            continue
        if f.fin:
            payload = b"".join(current["parts"])
            if current["rsv1"] and deflate is not None:   # without the extension RSV1 is only a violation
                if no_ctx:
                    inflater = zlib.decompressobj(-wbits)
                try:
                    payload = inflater.decompress(payload + b"\x00\x00\xff\xff")
                except zlib.error as exc:
                    violations.append({"offset": f.offset, "kind": f"inflate failed: {exc}"})
            messages.append(WsMessage(direction, conn, current["opcode"], payload, current["rsv1"],
                                      current["frames"], f.offset + f.header_len + f.length))
            current = None
    incomplete = None if current is None else {"frames": current["frames"],
                                               "bytes": sum(len(p) for p in current["parts"])}
    return messages, controls, violations, incomplete


# --------------------------------------------------------------------------- PDUs


def pdu_label(raw: bytes) -> dict:
    out: dict = {"len": len(raw)}
    if len(raw) < 8:
        out["kind"] = "short"
        return out
    total = raw[0] | raw[1] << 8 | raw[2] << 16
    u16_type = struct.unpack("<H", raw[4:6])[0]
    if u16_type > SEALED_TYPE_THRESHOLD:
        out["kind"] = "sealed_guess"
        return out
    msg_type, json_size = struct.unpack("<hh", raw[4:8])
    out.update({"kind": "pdu", "type": msg_type, "name": MESSAGE_NAMES.get(msg_type, f"type{msg_type}"),
                "total_size": total, "pdu_version": raw[3], "json_size": json_size,
                "tail_len": max(0, len(raw) - 8 - max(json_size, 0))})
    return out


def opt_int(obj: Optional[dict], key: str, default: int) -> int:
    """org.json optInt, with the same rules as plaudsim.wifi._opt_int (tests pin the equality):
    a bool gives the default; an int is kept; a finite float is truncated; a numeric string goes
    through float(); anything else, and any non-finite number, gives the default."""
    if obj is None or key not in obj:
        return default
    value = obj[key]
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return default
        return int(number) if math.isfinite(number) else default
    return default


def file_transfers(messages: list) -> dict:
    """Re-hash FileSyncContent tails the way the phone accepts them (plaudsim.wifi.FileSyncContent):
    the tail counts only when its length equals the JSON "length" and is > 0; bytes are appended in
    arrival order; "last" == 1 finishes a transfer; offset 0 after data (or a new session) starts one."""
    transfers: list = []
    cur: Optional[dict] = None
    sealed = 0
    for raw in messages:
        label = pdu_label(raw)
        if label.get("kind") == "sealed_guess":
            sealed += 1
            continue
        if label.get("kind") != "pdu" or label["type"] != MSG_FILE_SYNC_CONTENT:
            continue
        jsize = label["json_size"]
        try:
            obj = json.loads(raw[8:8 + jsize].decode("utf-8"))
            if not isinstance(obj, dict):
                obj = None
        except (UnicodeDecodeError, ValueError):    # BaseWifiResponse: unparsable JSON -> every getter's default
            obj = None

        session, offset = opt_int(obj, "session", 0), opt_int(obj, "offset", 0)
        length, last = opt_int(obj, "length", 0), opt_int(obj, "last", 0) == 1
        tail = raw[8 + jsize:]
        accepted = tail if 0 < len(tail) == length else b""
        if cur is None or cur["finished"] or session != cur["session"] or (offset == 0 and cur["bytes"] > 0):
            cur = {"session": session, "frames": 0, "bytes": 0, "rejected_tails": 0, "finished": False,
                   "_h": hashlib.sha256()}
            transfers.append(cur)
        cur["frames"] += 1
        cur["bytes"] += len(accepted)
        cur["_h"].update(accepted)
        if tail and not accepted:
            cur["rejected_tails"] += 1
        if last:
            cur["finished"] = True
    for t in transfers:
        t["sha256"] = t.pop("_h").hexdigest()
    return {"transfers": transfers, "sealed_messages": sealed}


def compare_sequences(pcap_msgs: list, json_msgs: list) -> dict:
    n = min(len(pcap_msgs), len(json_msgs))
    first = next((i for i in range(n) if pcap_msgs[i] != json_msgs[i]), None)
    result: dict = {"pcap_count": len(pcap_msgs), "json_count": len(json_msgs),
                    "match": first is None and len(pcap_msgs) == len(json_msgs)}
    if first is not None:
        a, b = pcap_msgs[first], json_msgs[first]
        diff_at = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
        result["first_mismatch"] = {"index": first, "byte_offset": diff_at,
                                    "pcap": {**pdu_label(a), "head_hex": a[:32].hex()},
                                    "json": {**pdu_label(b), "head_hex": b[:32].hex()}}
    elif len(pcap_msgs) != len(json_msgs):
        longer, which = (pcap_msgs, "pcap") if len(pcap_msgs) > len(json_msgs) else (json_msgs, "json")
        result["extra"] = {"in": which, "from_index": n, "labels": [pdu_label(m) for m in longer[n:n + 5]]}
    return result


# --------------------------------------------------------------------------- flows / egress


def _label(d: Decoded) -> str:
    ports = {d.sport, d.dport}
    if d.proto == "arp":
        return "arp"
    if d.proto == "udp" and ports & {67, 68, 546, 547}:
        return "dhcp"
    if ports & {53}:
        return "dns"
    if d.proto == "udp" and 5353 in ports:
        return "mdns"
    if d.proto == "icmpv6":
        return "icmpv6"
    if d.proto == "icmp":
        return "icmp"
    return d.proto


def other_flows(decoded: list, port: int, slirp_hosts: set) -> tuple:
    flows: dict = {}
    for d in decoded:
        if d.proto == "tcp" and port in (d.sport, d.dport):
            continue
        a, b = (d.src, d.sport), (d.dst, d.dport)
        key_ends = tuple(sorted([a, b], key=lambda e: (str(e[0]), e[1] or 0)))
        key = (d.l3, d.proto, key_ends)
        f = flows.get(key)
        if f is None:
            f = flows[key] = {"l3": d.l3, "proto": d.proto, "label": _label(d),
                              "a": {"addr": key_ends[0][0], "port": key_ends[0][1]},
                              "b": {"addr": key_ends[1][0], "port": key_ends[1][1]},
                              "packets_a_to_b": 0, "packets_b_to_a": 0, "bytes": 0,
                              "first_ts": d.ts, "last_ts": d.ts, "initiator": None,
                              "syn_ack": False, "rst": False, "fin": False, "icmp_types": []}
        forward = (d.src, d.sport) == key_ends[0]
        f["packets_a_to_b" if forward else "packets_b_to_a"] += 1
        f["bytes"] += d.length
        f["last_ts"] = d.ts
        if d.proto == "tcp":
            if d.flags & TCP_SYN and not d.flags & TCP_ACK and f["initiator"] is None:
                f["initiator"] = d.src
            if d.flags & TCP_SYN and d.flags & TCP_ACK:
                f["syn_ack"] = True
            f["rst"] = f["rst"] or bool(d.flags & TCP_RST)
            f["fin"] = f["fin"] or bool(d.flags & TCP_FIN)
        elif d.proto == "udp" and f["initiator"] is None:
            f["initiator"] = d.src
        elif d.proto in ("icmp", "icmpv6", "arp") and d.icmp_type is not None:
            if d.icmp_type not in f["icmp_types"]:
                f["icmp_types"].append(d.icmp_type)
            if d.proto in ("icmp", "icmpv6"):
                if d.icmp_type in ECHO_REQUEST and f["initiator"] is None:
                    f["initiator"] = d.src
                elif d.icmp_type in ECHO_REPLY and d.src not in f.setdefault("echo_reply_from", []):
                    f["echo_reply_from"].append(d.src)
    listed = sorted(flows.values(), key=lambda f: f["first_ts"])
    established, answered, pinged = [], [], []
    for f in listed:
        init = f["initiator"]
        if init is None or init in slirp_hosts or f["label"] == "dhcp":
            continue
        peer_to_init = f["packets_b_to_a"] if init == f["a"]["addr"] else f["packets_a_to_b"]
        peer = f["b"] if init == f["a"]["addr"] else f["a"]
        if f["proto"] == "tcp" and f["syn_ack"]:
            established.append({"from": init, "to": peer})
        elif f["proto"] == "udp" and peer_to_init > 0 and not str(peer["addr"]).startswith(("224.", "ff0")):
            answered.append({"from": init, "to": peer, "label": f["label"]})
        elif f["proto"] in ("icmp", "icmpv6") and peer["addr"] not in slirp_hosts \
                and peer["addr"] in f.get("echo_reply_from", []):
            # libslirp answers pings to its own addresses itself, restricted or not (src/ip_icmp.c:
            # icmp_reflect for vhost/vnameserver); a reply from anywhere else crossed the host.
            pinged.append({"from": init, "to": peer["addr"], "proto": f["proto"]})
    return listed, {"guest_tcp_answered": established, "guest_udp_answered": answered,
                    "guest_icmp_answered": pinged}


# --------------------------------------------------------------------------- /proc/net/tcp, logcat

_PROC_ROW = re.compile(r"^\s*\d+:\s+([0-9A-Fa-f]{8}|[0-9A-Fa-f]{32}):([0-9A-Fa-f]{4})\s+"
                       r"([0-9A-Fa-f]{8}|[0-9A-Fa-f]{32}):([0-9A-Fa-f]{4})\s+([0-9A-Fa-f]{2})\b")
TCP_STATES = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT",
              7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}


def decode_proc_addr(hexaddr: str) -> str:
    """/proc/net/tcp{,6} address: 32-bit words in host (little-endian) byte order."""
    raw = bytes.fromhex(hexaddr)
    swapped = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    if len(swapped) == 4:
        return str(ipaddress.IPv4Address(swapped))
    v6 = ipaddress.IPv6Address(swapped)
    return str(v6.ipv4_mapped) if v6.ipv4_mapped else str(v6)


def parse_proc_net_tcp(text: str) -> list:
    rows = []
    for line in text.splitlines():
        m = _PROC_ROW.match(line)
        if not m:
            continue
        state = int(m.group(5), 16)
        rows.append({"local": decode_proc_addr(m.group(1)), "local_port": int(m.group(2), 16),
                     "remote": decode_proc_addr(m.group(3)), "remote_port": int(m.group(4), 16),
                     "state": TCP_STATES.get(state, f"0x{state:02x}")})
    return rows


_DEVICE_CONNECTED = re.compile(r"Device connected:\s*(\S+)")


def parse_logcat(text: str) -> list:
    return [m.group(1) for m in _DEVICE_CONNECTED.finditer(text)]


# --------------------------------------------------------------------------- analysis


def _file_info(path: str) -> dict:
    p = Path(path)
    data = p.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def load_capture_json(path: str) -> tuple:
    try:
        doc = json.loads(Path(path).read_text())
    except OSError as exc:
        raise InputError(f"cannot read capture JSON {path}: {exc}") from exc
    except ValueError as exc:
        raise InputError(f"capture JSON {path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("wifi"), list):
        raise InputError(f"capture JSON {path} has no 'wifi' list")
    out, inn, times, issues, events = [], [], {"out": [], "in": []}, [], []
    for n, e in enumerate(doc["wifi"]):
        if isinstance(e, dict) and e.get("dir") == "event" and isinstance(e.get("kind"), str):
            events.append(e["kind"])
        if not isinstance(e, dict) or e.get("dir") not in ("out", "in"):
            continue
        try:
            raw = bytes.fromhex(e.get("hex", ""))
        except (TypeError, ValueError) as exc:
            raise InputError(f"capture JSON entry {n}: bad hex ({exc})") from exc
        if "len" in e and e["len"] != len(raw):
            issues.append({"entry": n, "kind": "len field != hex length", "len": e["len"], "hex_len": len(raw)})
        (out if e["dir"] == "out" else inn).append(raw)
        times[e["dir"]].append(e.get("t"))
    stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
    return {"out": out, "in": inn, "times": times, "stats": stats, "issues": issues, "events": events}


def summarise_pcap(pcap: Pcap) -> dict:
    ts = [p.ts for p in pcap.packets]
    return {"path": pcap.path, "linktype": pcap.linktype, "snaplen": pcap.snaplen, "packets": len(pcap.packets),
            "first_ts": min(ts) if ts else None, "last_ts": max(ts) if ts else None,
            "truncated_packets": sum(1 for p in pcap.packets if p.caplen < p.origlen),
            "file_truncated": pcap.file_truncated}


def _check(status: str, **detail: Any) -> dict:
    return {"status": status, **detail}


def analyse(args: argparse.Namespace) -> dict:
    slirp_hosts = set(args.slirp_hosts.split(",")) if args.slirp_hosts else set()
    report: dict = {"tool": "r7/r7-s16-evidence/check_wifi_pcap.py",
                    "mode": "summary" if args.summary else ("control" if args.expect_none else "transfer"),
                    "params": {"port": args.port, "server_ip": args.server_ip,
                               "expect_client_ip": args.expect_client_ip, "slirp_hosts": sorted(slirp_hosts)},
                    "inputs": {}, "checks": {}}
    checks = report["checks"]

    wifi = read_pcap(args.wifi_pcap)
    report["inputs"]["wifi_pcap"] = _file_info(args.wifi_pcap)
    report["wifi_pcap"] = summarise_pcap(wifi)
    decoded = [decode(p, wifi.linktype) for p in wifi.packets]

    # --- the --port stream(s)
    conns = collect_port_conns(decoded, args.port)
    conn_reports, out_msgs, in_msgs = [], [], []
    reassembly_problems, violations_all = [], []
    upgraded, payload_bytes = [], 0
    deflate_seen = False
    for ci, c in enumerate(conns):
        a_c2s, a_s2c = assemble(c.c2s), assemble(c.s2c)
        payload_bytes += len(a_c2s.data) + len(a_s2c.data) + a_c2s.unassembled_bytes + a_s2c.unassembled_bytes
        cr: dict = {"index": ci, "client": f"{c.client[0]}:{c.client[1]}", "server": f"{c.server[0]}:{c.server[1]}",
                    "first_ts": c.first_ts, "last_ts": c.last_ts, "syn_ack": c.syn_ack,
                    "client_packets": c.c2s.packets, "server_packets": c.s2c.packets,
                    "client_bytes": len(a_c2s.data), "server_bytes": len(a_s2c.data),
                    "client_fin": c.c2s.fin, "server_fin": c.s2c.fin,
                    "client_rst": c.c2s.rst, "server_rst": c.s2c.rst}
        issues = [dict(i, direction="c2s") for i in a_c2s.issues] + [dict(i, direction="s2c") for i in a_s2c.issues]
        truncated = c.c2s.truncated + c.s2c.truncated
        if truncated:
            issues.append({"kind": "truncated_packets", "count": truncated})
        cr["reassembly_issues"] = issues
        if issues:   # a gap, a conflicting overlap, a truncated packet, or a stream whose start is missing
            reassembly_problems.extend(dict(i, conn=ci) for i in issues)
        if not a_c2s.data and not a_s2c.data:
            cr["outcome"] = ("refused (RST from server)" if c.s2c.rst and not c.syn_ack
                             else "no payload")
            conn_reports.append(cr)
            continue
        req_line, req_headers, c_rest = split_http(a_c2s.data)
        rsp_line, rsp_headers, s_rest = split_http(a_s2c.data)
        cr["http_request"] = req_line
        cr["http_response"] = rsp_line
        cr["request_headers"] = {k: req_headers[k] for k in ("host", "upgrade", "sec-websocket-version",
                                                             "sec-websocket-extensions") if k in req_headers}
        cr["response_headers"] = {k: rsp_headers[k] for k in ("upgrade", "sec-websocket-accept",
                                                              "sec-websocket-extensions") if k in rsp_headers}
        is_ws = bool(req_line and req_line.startswith("GET ") and rsp_line and " 101" in rsp_line)
        cr["websocket"] = is_ws
        if not is_ws:
            cr["outcome"] = "payload without a WebSocket upgrade"
            conn_reports.append(cr)
            continue
        upgraded.append(c)
        deflate = parse_deflate(rsp_headers.get("sec-websocket-extensions"))
        deflate_seen = deflate_seen or deflate is not None
        cr["permessage_deflate"] = deflate is not None
        c_off = len(a_c2s.data) - len(c_rest)
        s_off = len(a_s2c.data) - len(s_rest)
        cf, c_trail, c_viol = parse_ws_frames(c_rest, c_off)
        sf, s_trail, s_viol = parse_ws_frames(s_rest, s_off)
        cm, cc, cv, c_inc = assemble_messages(cf, "out", ci, deflate)
        sm, sc, sv, s_inc = assemble_messages(sf, "in", ci, deflate)
        for m in cm:
            m.ts = ts_at(a_c2s.arrivals, m.end_offset - 1)
        for m in sm:
            m.ts = ts_at(a_s2c.arrivals, m.end_offset - 1)
        viol = [dict(v, direction="c2s") for v in c_viol + cv] + [dict(v, direction="s2c") for v in s_viol + sv]
        violations_all.extend(dict(v, conn=ci) for v in viol)
        tail_issues = []
        for direction, trail, inc in (("c2s", c_trail, c_inc), ("s2c", s_trail, s_inc)):
            if trail:
                tail_issues.append({"kind": "trailing_partial_frame", "bytes": trail, "direction": direction})
            if inc:
                tail_issues.append({"kind": "incomplete_message", "direction": direction, **inc})
        cr["reassembly_issues"].extend(tail_issues)
        reassembly_problems.extend(dict(i, conn=ci) for i in tail_issues)
        cr.update({"client_frames": len(cf), "server_frames": len(sf), "client_messages": len(cm),
                   "server_messages": len(sm), "client_controls": cc, "server_controls": sc,
                   "client_trailing_bytes": c_trail, "server_trailing_bytes": s_trail,
                   "client_incomplete_message": c_inc, "server_incomplete_message": s_inc,
                   "protocol_violations": viol, "outcome": "websocket"})
        out_msgs.extend(m for m in cm if m.opcode in (1, 2))
        in_msgs.extend(m for m in sm if m.opcode in (1, 2))
        conn_reports.append(cr)
    report["port_connections"] = conn_reports
    report["port_summary"] = {"connections": len(conns), "websocket_connections": len(upgraded),
                              "refused_or_empty": sum(1 for r in conn_reports if r.get("outcome") != "websocket"),
                              "payload_bytes": payload_bytes, "permessage_deflate": deflate_seen}
    report["messages"] = [{"dir": m.direction, "conn": m.conn, "ts": m.ts, "frames": m.frames,
                           "compressed": m.compressed, **pdu_label(m.payload)} for m in out_msgs + in_msgs]
    report["messages"].sort(key=lambda r: (r["ts"] is None, r["ts"] or 0.0))

    flows, egress = other_flows(decoded, args.port, slirp_hosts)
    report["other_flows"] = flows
    report["egress"] = egress

    # --- Ethernet negative control
    eth_hits = None
    if args.eth_pcap:
        eth = read_pcap(args.eth_pcap)
        report["inputs"]["eth_pcap"] = _file_info(args.eth_pcap)
        report["eth_pcap"] = summarise_pcap(eth)
        eth_dec = [decode(p, eth.linktype) for p in eth.packets]
        eth_hits = [{"packet": d.index, "ts": d.ts, "proto": d.proto, "src": f"{d.src}:{d.sport}",
                     "dst": f"{d.dst}:{d.dport}"}
                    for d in eth_dec if d.proto in ("tcp", "udp") and args.port in (d.sport, d.dport)]
        report["eth_pcap"]["port_packets"] = eth_hits[:20]
        report["eth_pcap"]["port_packet_count"] = len(eth_hits)
        eth_flows, _ = other_flows(eth_dec, -1, slirp_hosts)
        report["eth_pcap"]["flows"] = eth_flows

    if args.summary:
        report["verdict"], report["exit_code"] = "summary", EXIT_PASS
        return report

    # --- optional text inputs
    proc_rows = None
    if args.proc_net_tcp:
        try:
            text = Path(args.proc_net_tcp).read_text(errors="replace")
        except OSError as exc:
            raise InputError(f"cannot read {args.proc_net_tcp}: {exc}") from exc
        report["inputs"]["proc_net_tcp"] = _file_info(args.proc_net_tcp)
        proc_rows = [r for r in parse_proc_net_tcp(text) if args.port in (r["local_port"], r["remote_port"])]
        uniq = []
        for r in proc_rows:
            if r not in uniq:
                uniq.append(r)
        report["proc_net_tcp_rows"] = uniq
    connected = logcat_text = None
    if args.logcat:
        try:
            logcat_text = Path(args.logcat).read_text(errors="replace")
        except OSError as exc:
            raise InputError(f"cannot read {args.logcat}: {exc}") from exc
        report["inputs"]["logcat"] = _file_info(args.logcat)
        connected = parse_logcat(logcat_text)
        report["logcat_device_connected"] = connected
    wifi_status_text = None
    if args.wifi_status:
        try:
            wifi_status_text = Path(args.wifi_status).read_text(errors="replace")
        except OSError as exc:
            raise InputError(f"cannot read {args.wifi_status}: {exc}") from exc
        report["inputs"]["wifi_status"] = _file_info(args.wifi_status)
        report["wifi_status_on_ssid"] = f'connected to "{args.ssid}"' in wifi_status_text
    json_out = json_in = json_events = None
    expected_sha = args.expected_sha256
    if args.capture_json:
        cap = load_capture_json(args.capture_json)
        json_out, json_in, times, stats, json_events = cap["out"], cap["in"], cap["times"], cap["stats"], cap["events"]
        report["inputs"]["capture_json"] = _file_info(args.capture_json)
        report["capture_json"] = {"out": len(json_out), "in": len(json_in), "issues": cap["issues"],
                                  "events": {k: json_events.count(k) for k in sorted(set(json_events))},
                                  "stats_file_sha256": stats.get("file_sha256"), "t0": stats.get("t0")}
        expected_sha = expected_sha or stats.get("file_sha256")
        t0 = stats.get("t0")
        if isinstance(t0, (int, float)) and not args.expect_none:
            skews = []
            for msgs, key in ((out_msgs, "out"), (in_msgs, "in")):
                for m, t in zip(msgs, times[key]):
                    if m.ts is not None and isinstance(t, (int, float)):
                        skews.append(t0 + t - m.ts)
            if skews:
                report["capture_json"]["json_minus_pcap_seconds"] = {"min": min(skews), "max": max(skews)}

    # --- checks
    capture_defect = bool(reassembly_problems) or wifi.file_truncated
    wifi_unusable = [r for r, bad in (("pcap file truncated", wifi.file_truncated),
                                      ("pcap holds no packet", not wifi.packets)) if bad]
    if args.expect_none:
        if payload_bytes or upgraded:
            status = "fail"
        else:
            status = "inconclusive" if wifi_unusable else "pass"
        checks["no_port_payload"] = _check(status, payload_bytes=payload_bytes, websocket_connections=len(upgraded),
                                           connection_attempts=len(conns), unusable=wifi_unusable)
        # absence proves nothing unless the run was live: the guest used the Wi-Fi netdev, the SDK's
        # server listened, the pen dialled, and the phone was on the pen's network
        missing: list = list(wifi_unusable)
        guest_packets = sum(1 for d in decoded if args.server_ip in (d.src, d.dst))
        if not guest_packets:
            missing.append(f"no packet to or from {args.server_ip} in the Wi-Fi pcap")
        if proc_rows is None:
            missing.append("no --proc-net-tcp")
        elif not any(r["state"] == "LISTEN" and r["local_port"] == args.port for r in proc_rows):
            missing.append(f"no LISTEN row on port {args.port} in the guest")
        if logcat_text is None:
            missing.append("no --logcat")
        elif not re.search(rf"Starting WebSocket server on port {args.port}\b", logcat_text):
            missing.append(f"no 'Starting WebSocket server on port {args.port}' in the logcat")
        if json_events is None:
            missing.append("no --capture-json")
        elif not any(k in ("connect_retry", "connect_failed") for k in json_events):
            missing.append("the pen logged no dial attempt (connect_retry/connect_failed)")
        if wifi_status_text is not None and f'connected to "{args.ssid}"' not in wifi_status_text:
            missing.append(f"--wifi-status does not show the phone on {args.ssid}")
        checks["control_liveness"] = _check("inconclusive" if missing else "pass", missing=missing,
                                            guest_packets=guest_packets)
        if json_out is not None:
            n = len(json_out) + len(json_in)
            checks["capture_json_no_frames"] = _check("pass" if n == 0 else "fail", data_entries=n)
        if proc_rows is not None:
            est = [r for r in proc_rows if r["state"] == "ESTABLISHED" and r["local_port"] == args.port]
            checks["proc_net_tcp_no_established"] = _check("pass" if not est else "fail", rows=est[:5])
        if connected is not None:
            checks["logcat_no_device_connected"] = _check("pass" if not connected else "fail", peers=connected)
    else:
        checks["ws_stream_found"] = _check("pass" if upgraded else "fail", websocket_connections=len(upgraded),
                                           connection_attempts=len(conns))
        bad_ends = [f"{c.client[0]} -> {c.server[0]}:{c.server[1]}" for c in upgraded
                    if c.client[0] != args.expect_client_ip or c.server[0] != args.server_ip]
        checks["ws_endpoints"] = _check("fail" if bad_ends else ("pass" if upgraded else "skipped"),
                                        unexpected=bad_ends)
        if capture_defect:
            checks["tcp_reassembly_clean"] = _check("inconclusive", issues=reassembly_problems[:20],
                                                    pcap_file_truncated=wifi.file_truncated)
        else:
            checks["tcp_reassembly_clean"] = _check("pass" if upgraded else "skipped")
        checks["ws_protocol"] = _check("fail" if violations_all else ("pass" if upgraded else "skipped"),
                                       violations=violations_all[:20])
        if json_out is not None:
            for name, pcap_side, json_side in (("pdu_out_match", out_msgs, json_out),
                                                ("pdu_in_match", in_msgs, json_in)):
                cmp = compare_sequences([m.payload for m in pcap_side], json_side)
                status = "pass" if cmp["match"] else ("inconclusive" if capture_defect else "fail")
                checks[name] = _check(status, **cmp)
        else:
            checks["pdu_out_match"] = _check("skipped", reason="no --capture-json")
            checks["pdu_in_match"] = _check("skipped", reason="no --capture-json")
        ft = file_transfers([m.payload for m in out_msgs])
        report["file_transfers"] = ft
        if json_out is not None:
            report["file_transfers_capture_json"] = file_transfers(json_out)
        finished = [t for t in ft["transfers"] if t["finished"]]
        unfinished = [t for t in ft["transfers"] if not t["finished"]]
        if not expected_sha:
            checks["file_sha256"] = _check("skipped", reason="no expected sha256")
        elif not finished:
            checks["file_sha256"] = _check("inconclusive" if ft["sealed_messages"] or capture_defect else "fail",
                                           reason="no finished FileSyncContent transfer", **ft)
        else:
            bad = [t for t in finished if t["sha256"] != expected_sha]
            status = "pass" if not bad else ("inconclusive" if capture_defect else "fail")
            checks["file_sha256"] = _check(status, expected=expected_sha, finished=finished,
                                           unfinished_not_judged=unfinished)
        if proc_rows is not None:
            hit = [r for r in proc_rows if r["state"] == "ESTABLISHED" and r["local"] == args.server_ip
                   and r["local_port"] == args.port and r["remote"] == args.expect_client_ip]
            checks["proc_net_tcp_peer"] = _check("pass" if hit else "fail", matching_rows=hit[:5],
                                                 port_rows=len(proc_rows))
        else:
            checks["proc_net_tcp_peer"] = _check("skipped", reason="no --proc-net-tcp")
        if connected is not None:
            wrong = [p for p in connected if p != args.expect_client_ip]
            checks["logcat_device_connected"] = _check("fail" if wrong or not connected else "pass",
                                                       peers=connected)
        else:
            checks["logcat_device_connected"] = _check("skipped", reason="no --logcat")
    if eth_hits is not None:
        eth_unusable = [r for r, bad in (("pcap file truncated", eth.file_truncated),
                                         ("pcap holds no packet", not eth.packets)) if bad]
        status = "fail" if eth_hits else ("inconclusive" if eth_unusable else "pass")
        checks["eth_no_port"] = _check(status, port_packets=len(eth_hits), unusable=eth_unusable)
    else:
        checks["eth_no_port"] = _check("skipped", reason="no --eth-pcap")
    if args.expect_no_egress:
        bad = egress["guest_tcp_answered"] + egress["guest_udp_answered"] + egress["guest_icmp_answered"]
        checks["egress_none"] = _check("pass" if not bad else "fail", answered=bad)

    statuses = [c["status"] for c in checks.values()]
    if "fail" in statuses:
        report["verdict"], report["exit_code"] = "fail", EXIT_FAIL
    elif "inconclusive" in statuses:
        report["verdict"], report["exit_code"] = "inconclusive", EXIT_INCONCLUSIVE
    else:
        report["verdict"], report["exit_code"] = "pass", EXIT_PASS
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wifi-pcap", required=True, help="filter-dump pcap of netdev virtio-wifi")
    p.add_argument("--capture-json", help="the pen's capture JSON (wifi_capture_device.py)")
    p.add_argument("--eth-pcap", help="pcap of the Ethernet netdev mynet (negative control)")
    p.add_argument("--proc-net-tcp", help="polled /proc/net/tcp{,6} rows from the guest")
    p.add_argument("--logcat", help="logcat of the run")
    p.add_argument("--wifi-status", help="`cmd wifi status` taken while the phone should be on --ssid")
    p.add_argument("--ssid", default="PLAUD0001")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--server-ip", default="10.0.2.16")
    p.add_argument("--expect-client-ip", default="10.0.2.2")
    p.add_argument("--slirp-hosts", default="10.0.2.2,10.0.2.3,fec0::2,fec0::3,fe80::2",
                   help="addresses of the user-mode network itself (not counted as guest egress)")
    p.add_argument("--expected-sha256", help="served file sha256 (default: capture JSON stats.file_sha256)")
    p.add_argument("--expect-none", action="store_true", help="control run: no traffic on --port expected")
    p.add_argument("--expect-no-egress", action="store_true", help="fail on any answered guest-initiated flow")
    p.add_argument("--summary", action="store_true", help="only parse and summarise the pcaps")
    p.add_argument("--report", help="write the JSON report here (default: stdout)")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = analyse(args)
    except Exception as exc:   # InputError, or a checker bug: never let a traceback read as "fail" (1)
        if isinstance(exc, InputError):
            err = {"verdict": "input_error", "error": str(exc)}
        else:
            err = {"verdict": "internal_error", "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-4000:]}
        err = {"tool": "r7/r7-s16-evidence/check_wifi_pcap.py", **err, "exit_code": EXIT_USAGE}
        if args.report:
            try:
                Path(args.report).write_text(json.dumps(err, indent=1) + "\n")
            except OSError:
                pass
        print(json.dumps(err), file=sys.stderr)
        return EXIT_USAGE
    text = json.dumps(report, indent=1, default=str) + "\n"
    if args.report:
        try:
            Path(args.report).write_text(text)
        except OSError as exc:
            print(json.dumps({"verdict": "input_error", "error": f"cannot write report: {exc}"}), file=sys.stderr)
            return EXIT_USAGE
        failing = [k for k, v in report["checks"].items() if v["status"] in ("fail", "inconclusive")]
        print(f"verdict={report['verdict']} exit={report['exit_code']} "
              f"not_passed={','.join(failing) or '-'} report={args.report}")
    else:
        sys.stdout.write(text)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
