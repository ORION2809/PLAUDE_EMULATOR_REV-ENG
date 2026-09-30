"""R7-S16 evidence checker (r7/r7-s16-evidence/check_wifi_pcap.py) against synthetic pcaps.

The run itself (an AVD whose Wi-Fi netdev carries the pen's WebSocket) has not happened yet; these
tests pin the checker: pcap parsing (Ethernet padding, IPv4, TCP), reassembly of both directions of
the port-8081 stream (split, out-of-order and retransmitted segments, a gap), WebSocket framing
(client masking, fragmentation with an interleaved control frame, 16- and 64-bit lengths,
permessage-deflate), the PDU comparison with the pen's capture JSON, the FileSyncContent re-hash,
the other-flows (egress) record, and the Ethernet-netdev negative control.

The PDUs are built with plaudsim.wifi, so the checker's stdlib PDU reader is also cross-checked
against the emulator's codec.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))

from plaudsim.wifi import (  # noqa: E402
    MESSAGE_NAMES,
    MSG_FILE_SYNC,
    MSG_HANDSHAKE,
    MSG_SAY_HELLO,
    FileSyncContent,
    pack_pdu,
)

CHECKER_PATH = ROOT / "r7" / "r7-s16-evidence" / "check_wifi_pcap.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("r7_s16_check_wifi_pcap", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


chk = _load_checker()

# --------------------------------------------------------------------------- packet builders

GUEST_MAC = bytes.fromhex("021500000000")
SLIRP_MAC = bytes.fromhex("525400123502")
PEN_IP, PHONE_IP = "10.0.2.2", "10.0.2.16"


def _ip(addr: str) -> bytes:
    return bytes(int(x) for x in addr.split("."))


def _csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f">{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def ipv4(src: str, dst: str, proto: int, l4: bytes) -> bytes:
    head = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(l4), 1, 0x4000, 64, proto, 0, _ip(src), _ip(dst))
    return head[:10] + struct.pack(">H", _csum(head)) + head[12:] + l4


def tcp(sport: int, dport: int, seq: int, ack: int, flags: int, payload: bytes = b"") -> bytes:
    return struct.pack(">HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags,
                       65535, 0, 0) + payload


def udp(sport: int, dport: int, payload: bytes) -> bytes:
    return struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload


def eth(src_ip: str, ip_packet: bytes) -> bytes:
    src, dst = (GUEST_MAC, SLIRP_MAC) if src_ip == PHONE_IP else (SLIRP_MAC, GUEST_MAC)
    frame = dst + src + b"\x08\x00" + ip_packet
    return frame + b"\0" * max(0, 60 - len(frame))     # Ethernet minimum-size padding


def pcap_bytes(frames: list) -> bytes:
    out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)]
    for ts, frame in frames:
        sec = int(ts)
        out.append(struct.pack("<IIII", sec, int(round((ts - sec) * 1e6)), len(frame), len(frame)) + frame)
    return b"".join(out)


def ws_frame(payload: bytes, opcode: int = 2, fin: bool = True, mask: bytes | None = None,
             rsv1: bool = False) -> bytes:
    b0 = (0x80 if fin else 0) | (0x40 if rsv1 else 0) | opcode
    n = len(payload)
    if n < 126:
        head = bytes([b0, (0x80 if mask else 0) | n])
    elif n < 65536:
        head = bytes([b0, (0x80 if mask else 0) | 126]) + struct.pack(">H", n)
    else:
        head = bytes([b0, (0x80 if mask else 0) | 127]) + struct.pack(">Q", n)
    if mask:
        return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + payload


# --------------------------------------------------------------------------- the synthetic session

FILE = bytes(range(256)) * 3                      # 768 bytes, served in two FileSyncContent PDUs
FILE_SHA = hashlib.sha256(FILE).hexdigest()
SAY_HELLO = pack_pdu(MSG_SAY_HELLO, {"sn": "8810000001", "version": "V0001", "token": "T", "pVer": 7})
HS_REQ = pack_pdu(MSG_HANDSHAKE, {"token": "0" * 32, "stamp": 1790680685})
HS_RSP = pack_pdu(MSG_HANDSHAKE, {"status": 0, "session": "SYN-WIFI-SESSION-0001"})
FILE_SYNC_REQ = pack_pdu(MSG_FILE_SYNC, {"session": 1700000000, "scene": 2, "start": 0, "end": len(FILE)})
CONTENT_1 = FileSyncContent(1700000000, 0, FILE[:500], False).encode()
CONTENT_2 = FileSyncContent(1700000000, 500, FILE[500:], True).encode()
PEN_OUT = [SAY_HELLO, HS_RSP, CONTENT_1, CONTENT_2]
PHONE_OUT = [HS_REQ, FILE_SYNC_REQ]

HTTP_REQ = (b"GET / HTTP/1.1\r\nHost: 127.0.0.1:18081\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
HTTP_RSP = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
T0 = 1790680600.0


def client_stream(before_content: tuple = ()) -> bytes:
    """Masked client frames; CONTENT_1 is fragmented with a ping between its two frames, and
    CONTENT_2 needs a 16-bit length. `before_content` adds whole messages after HS_RSP."""
    half = len(CONTENT_1) // 2
    return (HTTP_REQ
            + ws_frame(SAY_HELLO, mask=b"\x11\x22\x33\x44")
            + ws_frame(HS_RSP, mask=b"\x55\x66\x77\x88")
            + b"".join(ws_frame(m, mask=b"\x21\x43\x65\x87") for m in before_content)
            + ws_frame(CONTENT_1[:half], fin=False, mask=b"\x01\x02\x03\x04")
            + ws_frame(b"hb", opcode=9, mask=b"\x09\x09\x09\x09")
            + ws_frame(CONTENT_1[half:], opcode=0, mask=b"\x05\x06\x07\x08")
            + ws_frame(CONTENT_2, mask=b"\xaa\xbb\xcc\xdd"))


def server_stream() -> bytes:
    return HTTP_RSP + ws_frame(HS_REQ) + ws_frame(FILE_SYNC_REQ)


def build_session(drop_client_segment: int | None = None, extra: list | None = None,
                  before_content: tuple = ()) -> bytes:
    """One refused dial, then the WebSocket session, plus unrelated guest traffic. The phone's last
    ACK covers the whole client stream and its FIN, as a real close does."""
    t = T0 + 40.0
    frames: list = []

    def add(src: str, packet: bytes) -> None:
        nonlocal t
        t += 0.001
        frames.append((t, eth(src, packet)))

    # a dial before the server existed: SYN, answered with RST
    add(PEN_IP, ipv4(PEN_IP, PHONE_IP, 6, tcp(40000, 8081, 777, 0, chk.TCP_SYN)))
    add(PHONE_IP, ipv4(PHONE_IP, PEN_IP, 6, tcp(8081, 40000, 0, 778, chk.TCP_RST | chk.TCP_ACK)))
    # unrelated guest traffic: an unanswered TCP SYN and an unanswered DNS query
    add(PHONE_IP, ipv4(PHONE_IP, "1.1.1.1", 6, tcp(50000, 443, 9, 0, chk.TCP_SYN)))
    add(PHONE_IP, ipv4(PHONE_IP, "10.0.2.3", 17, udp(40500, 53, b"\x12\x34" + b"\0" * 10)))
    # the session
    c_isn, s_isn = 1000, 5000
    add(PEN_IP, ipv4(PEN_IP, PHONE_IP, 6, tcp(40001, 8081, c_isn, 0, chk.TCP_SYN)))
    add(PHONE_IP, ipv4(PHONE_IP, PEN_IP, 6, tcp(8081, 40001, s_isn, c_isn + 1, chk.TCP_SYN | chk.TCP_ACK)))
    add(PEN_IP, ipv4(PEN_IP, PHONE_IP, 6, tcp(40001, 8081, c_isn + 1, s_isn + 1, chk.TCP_ACK)))  # padded ACK
    cs, ss = client_stream(before_content), server_stream()
    # client segments cut at awkward places (inside the HTTP header, inside frame headers)
    cuts = [0, 37, len(HTTP_REQ) + 1, len(HTTP_REQ) + 60, len(HTTP_REQ) + 61, 400, 777, len(cs)]
    segs = [(cuts[i], cs[cuts[i]:cuts[i + 1]]) for i in range(len(cuts) - 1)]
    order = [0, 2, 1, 3, 4, 4, 5, 6]      # out of order (2 before 1) and one retransmission (4 twice)
    s_segs = [(0, ss[:50]), (50, ss[50:])]
    for n, i in enumerate(order):
        if drop_client_segment is not None and i == drop_client_segment:
            continue
        off, data = segs[i]
        add(PEN_IP, ipv4(PEN_IP, PHONE_IP, 6, tcp(40001, 8081, c_isn + 1 + off, s_isn + 1,
                                                  chk.TCP_ACK | chk.TCP_PSH, data)))
        if n < len(s_segs):
            soff, sdata = s_segs[n]
            add(PHONE_IP, ipv4(PHONE_IP, PEN_IP, 6, tcp(8081, 40001, s_isn + 1 + soff, c_isn + 1,
                                                        chk.TCP_ACK | chk.TCP_PSH, sdata)))
    add(PEN_IP, ipv4(PEN_IP, PHONE_IP, 6, tcp(40001, 8081, c_isn + 1 + len(cs), s_isn + 1 + len(ss),
                                              chk.TCP_FIN | chk.TCP_ACK)))
    add(PHONE_IP, ipv4(PHONE_IP, PEN_IP, 6, tcp(8081, 40001, s_isn + 1 + len(ss), c_isn + 1 + len(cs) + 1,
                                                chk.TCP_ACK)))
    for src, packet in extra or []:
        add(src, packet)
    return pcap_bytes(frames)


def capture_json(out: list = PEN_OUT, inn: list = PHONE_OUT) -> dict:
    wifi = [{"t": 36.49, "dir": "event", "kind": "device_started", "uri": "ws://127.0.0.1:18081"}]
    t = 40.01
    for n in range(max(len(out), len(inn))):
        if n < len(out):
            wifi.append({"t": round(t, 3), "dir": "out", "len": len(out[n]), "hex": out[n].hex()})
            t += 0.005
        if n < len(inn):
            wifi.append({"t": round(t, 3), "dir": "in", "len": len(inn[n]), "hex": inn[n].hex()})
            t += 0.005
    return {"stats": {"file_sha256": FILE_SHA, "t0": T0, "host_port": 18081}, "wifi": wifi}


def eth_pcap(with_port_packet: bool = False) -> bytes:
    frames = [(T0 + 1, eth(PHONE_IP, ipv4("10.0.2.15", "10.0.2.3", 17, udp(41000, 53, b"q" * 12))))]
    if with_port_packet:
        frames.append((T0 + 2, eth(PEN_IP, ipv4(PEN_IP, "10.0.2.15", 6, tcp(40002, 8081, 1, 0, chk.TCP_SYN)))))
    return pcap_bytes(frames)


PROC_ROWS = (
    "12:34:56\n"
    "   0: 00000000000000000000000000000000:1F91 00000000000000000000000000000000:0000 0A 00000000:00000000\n"
    "   1: 0000000000000000FFFF00001002000A:1F91 0000000000000000FFFF00000202000A:9C41 01 00000000:00000000\n"
)
LOGCAT = ("09-30 12:00:00.000  4165  4212 I WebSocketOperation: Starting WebSocket server on port 8081\n"
          "09-30 12:00:01.000  4165  4322 I WebSocketOperation: Device connected: 10.0.2.2\n"
          "09-30 12:00:01.000  4165  4322 I WifiAgentImpl: WebSocket device connected\n")


def write_inputs(tmp_path: Path, pcap: bytes | None = None, cap: dict | None = None,
                 eth: bytes | None = None, logcat: str = LOGCAT, proc: str = PROC_ROWS) -> list:
    (tmp_path / "wifi.pcap").write_bytes(build_session() if pcap is None else pcap)
    (tmp_path / "capture.json").write_text(json.dumps(capture_json() if cap is None else cap))
    (tmp_path / "eth.pcap").write_bytes(eth_pcap() if eth is None else eth)
    (tmp_path / "logcat.log").write_text(logcat)
    (tmp_path / "proc.txt").write_text(proc)
    return ["--wifi-pcap", str(tmp_path / "wifi.pcap"), "--capture-json", str(tmp_path / "capture.json"),
            "--eth-pcap", str(tmp_path / "eth.pcap"), "--logcat", str(tmp_path / "logcat.log"),
            "--proc-net-tcp", str(tmp_path / "proc.txt"), "--report", str(tmp_path / "report.json")]


def run(tmp_path: Path, argv: list) -> tuple:
    code = chk.main(argv)
    return code, json.loads((tmp_path / "report.json").read_text())


# --------------------------------------------------------------------------- tests


def test_message_names_match_the_emulator_codec():
    assert chk.MESSAGE_NAMES == MESSAGE_NAMES


def test_matching_session_passes_every_check(tmp_path):
    code, rep = run(tmp_path, write_inputs(tmp_path) + ["--expect-no-egress"])
    assert code == chk.EXIT_PASS, rep["checks"]
    assert {k: v["status"] for k, v in rep["checks"].items()} == {
        "ws_stream_found": "pass", "ws_endpoints": "pass", "tcp_reassembly_clean": "pass",
        "ws_protocol": "pass", "pdu_out_match": "pass", "pdu_in_match": "pass", "file_sha256": "pass",
        "proc_net_tcp_peer": "pass", "logcat_device_connected": "pass", "eth_no_port": "pass",
        "egress_none": "pass"}
    summary = rep["port_summary"]
    assert (summary["connections"], summary["websocket_connections"], summary["refused_or_empty"]) == (2, 1, 1)
    assert rep["port_connections"][0]["outcome"] == "refused (RST from server)"
    ws = rep["port_connections"][1]
    assert ws["client"] == "10.0.2.2:40001" and ws["server"] == "10.0.2.16:8081"
    assert ws["client_controls"] == [{"offset": ws["client_controls"][0]["offset"], "opcode": "ping", "len": 2}]
    assert (ws["client_messages"], ws["server_messages"]) == (4, 2)
    assert [m["name"] for m in rep["messages"] if m["dir"] == "out"] == [
        "SayHello", "Handshake", "FileSyncContent", "FileSyncContent"]
    fragmented = [m for m in rep["messages"] if m["dir"] == "out" and m["frames"] == 2]
    assert len(fragmented) == 1 and fragmented[0]["tail_len"] == 500
    assert rep["file_transfers"]["transfers"] == [
        {"session": 1700000000, "frames": 2, "bytes": len(FILE), "rejected_tails": 0, "finished": True,
         "sha256": FILE_SHA}]
    skew = rep["capture_json"]["json_minus_pcap_seconds"]
    assert -1.0 < skew["min"] <= skew["max"] < 1.0


def test_other_flows_are_listed_as_the_egress_record(tmp_path):
    code, rep = run(tmp_path, write_inputs(tmp_path))
    assert code == chk.EXIT_PASS
    flows = {(f["label"], f["b"]["addr"] if f["a"]["addr"] == PHONE_IP else f["a"]["addr"]): f
             for f in rep["other_flows"]}
    syn = flows[("tcp", "1.1.1.1")]
    assert syn["initiator"] == PHONE_IP and not syn["syn_ack"] and syn["packets_b_to_a"] + syn["packets_a_to_b"] == 1
    assert flows[("dns", "10.0.2.3")]["initiator"] == PHONE_IP
    assert rep["egress"] == {"guest_tcp_answered": [], "guest_udp_answered": [], "guest_icmp_answered": []}


def test_answered_guest_connection_fails_the_egress_check(tmp_path):
    answer = (("1.1.1.1", ipv4("1.1.1.1", PHONE_IP, 6, tcp(443, 50000, 1, 10, chk.TCP_SYN | chk.TCP_ACK))),)
    args = write_inputs(tmp_path, pcap=build_session(extra=list(answer))) + ["--expect-no-egress"]
    code, rep = run(tmp_path, args)
    assert code == chk.EXIT_FAIL
    assert rep["checks"]["egress_none"]["status"] == "fail"
    assert rep["checks"]["egress_none"]["answered"] == [{"from": PHONE_IP, "to": {"addr": "1.1.1.1", "port": 443}}]


def test_one_changed_byte_in_the_capture_fails_the_out_comparison(tmp_path):
    changed = bytearray(HS_RSP)
    changed[-2] ^= 0x01
    cap = capture_json(out=[SAY_HELLO, bytes(changed), CONTENT_1, CONTENT_2])
    code, rep = run(tmp_path, write_inputs(tmp_path, cap=cap))
    assert code == chk.EXIT_FAIL
    out = rep["checks"]["pdu_out_match"]
    assert out["status"] == "fail" and out["first_mismatch"]["index"] == 1
    assert out["first_mismatch"]["byte_offset"] == len(HS_RSP) - 2
    assert rep["checks"]["pdu_in_match"]["status"] == "pass"


def test_a_message_missing_from_the_capture_fails_the_in_comparison(tmp_path):
    code, rep = run(tmp_path, write_inputs(tmp_path, cap=capture_json(inn=[HS_REQ])))
    assert code == chk.EXIT_FAIL
    cmp = rep["checks"]["pdu_in_match"]
    assert (cmp["status"], cmp["pcap_count"], cmp["json_count"]) == ("fail", 2, 1)
    assert cmp["extra"]["in"] == "pcap" and cmp["extra"]["labels"][0]["name"] == "FileSync"


def test_wrong_file_hash_fails(tmp_path):
    cap = capture_json()
    cap["stats"]["file_sha256"] = "0" * 64
    code, rep = run(tmp_path, write_inputs(tmp_path, cap=cap))
    assert code == chk.EXIT_FAIL and rep["checks"]["file_sha256"]["status"] == "fail"


def test_port_traffic_on_the_ethernet_netdev_fails_the_negative_control(tmp_path):
    code, rep = run(tmp_path, write_inputs(tmp_path, eth=eth_pcap(with_port_packet=True)))
    assert code == chk.EXIT_FAIL
    assert rep["checks"]["eth_no_port"] == {"status": "fail", "port_packets": 1, "unusable": []}
    assert rep["eth_pcap"]["port_packets"][0]["dst"] == "10.0.2.15:8081"


def test_wrong_peer_in_logcat_and_proc_fails(tmp_path):
    args = write_inputs(tmp_path, logcat=LOGCAT.replace("Device connected: 10.0.2.2", "Device connected: 127.0.0.1"),
                        proc=PROC_ROWS.replace("0202000A:9C41 01", "0100007F:9C41 01"))
    code, rep = run(tmp_path, args)
    assert code == chk.EXIT_FAIL
    assert rep["checks"]["logcat_device_connected"] == {"status": "fail", "peers": ["127.0.0.1"]}
    assert rep["checks"]["proc_net_tcp_peer"]["status"] == "fail"


def test_a_missing_segment_makes_the_run_inconclusive(tmp_path):
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=build_session(drop_client_segment=5)))
    assert code == chk.EXIT_INCONCLUSIVE, rep["checks"]
    assert rep["checks"]["tcp_reassembly_clean"]["status"] == "inconclusive"
    assert any(i["kind"] == "gap" for i in rep["checks"]["tcp_reassembly_clean"]["issues"])
    assert rep["checks"]["pdu_out_match"]["status"] == "inconclusive"


QUIET = pcap_bytes([(T0 + 1, eth(PHONE_IP, ipv4(PHONE_IP, "10.0.2.3", 17, udp(40500, 53, b"q" * 12))))])
CONTROL_CAP = {"stats": {"file_sha256": FILE_SHA, "t0": T0},
               "wifi": [{"t": 1.0, "dir": "event", "kind": "connect_retry", "attempt": 1}]}
NO_PEER_LOGCAT = "09-30 12:00:00.000 I WebSocketOperation: Starting WebSocket server on port 8081\n"
LISTEN_ONLY = PROC_ROWS.splitlines()[0] + "\n" + PROC_ROWS.splitlines()[1] + "\n"
WIFI_STATUS = 'Wifi is enabled\nWifi is connected to "PLAUD0001"\nWifiInfo: SSID: "PLAUD0001", IP: /10.0.2.16\n'


def control_args(tmp_path: Path, **overrides) -> list:
    """A live control run (every liveness sign present) unless an override removes one."""
    (tmp_path / "status.txt").write_text(overrides.pop("status", WIFI_STATUS))
    kw = dict(pcap=QUIET, cap=CONTROL_CAP, logcat=NO_PEER_LOGCAT, proc=LISTEN_ONLY)
    kw.update(overrides)
    return write_inputs(tmp_path, **kw) + ["--expect-none", "--wifi-status", str(tmp_path / "status.txt")]


def test_control_run_expects_no_port_traffic(tmp_path):
    code, rep = run(tmp_path, control_args(tmp_path))
    assert code == chk.EXIT_PASS, rep["checks"]
    assert set(rep["checks"]) == {"no_port_payload", "control_liveness", "capture_json_no_frames",
                                  "proc_net_tcp_no_established", "logcat_no_device_connected", "eth_no_port"}
    code, rep = run(tmp_path, write_inputs(tmp_path) + ["--expect-none"])
    assert code == chk.EXIT_FAIL and rep["checks"]["no_port_payload"]["status"] == "fail"


def test_control_run_on_empty_inputs_is_inconclusive_not_a_pass(tmp_path):
    """Review finding 4/8: header-only pcaps, no pen events, an empty logcat and no proc rows used
    to pass every --expect-none check."""
    empty = pcap_bytes([])
    args = write_inputs(tmp_path, pcap=empty, eth=empty, cap={"wifi": [], "stats": {}}, logcat="", proc="")
    code, rep = run(tmp_path, args + ["--expect-none", "--expect-no-egress"])
    assert code == chk.EXIT_INCONCLUSIVE, rep["checks"]
    assert rep["checks"]["no_port_payload"]["status"] == "inconclusive"
    assert rep["checks"]["eth_no_port"]["status"] == "inconclusive"
    missing = " | ".join(rep["checks"]["control_liveness"]["missing"])
    for sign in ("pcap holds no packet", "no packet to or from 10.0.2.16", "no LISTEN row on port 8081",
                 "Starting WebSocket server", "no dial attempt"):
        assert sign in missing


@pytest.mark.parametrize("override, sign", [
    ({"proc": PROC_ROWS.splitlines()[0] + "\n"}, "no LISTEN row"),
    ({"logcat": "nothing here\n"}, "Starting WebSocket server"),
    ({"cap": {"stats": {}, "wifi": [{"dir": "event", "kind": "device_started"}]}}, "no dial attempt"),
    ({"status": "Wifi is enabled\nWifi is disconnected\n"}, "not show the phone on PLAUD0001"),
    ({"pcap": pcap_bytes([(T0, eth(PEN_IP, ipv4(PEN_IP, "10.0.2.3", 17, udp(1, 53, b"x"))))])},
     "no packet to or from 10.0.2.16"),
])
def test_control_liveness_needs_every_sign(tmp_path, override, sign):
    code, rep = run(tmp_path, control_args(tmp_path, **override))
    assert code == chk.EXIT_INCONCLUSIVE, rep["checks"]
    assert rep["checks"]["control_liveness"]["status"] == "inconclusive"
    assert any(sign in m for m in rep["checks"]["control_liveness"]["missing"])


def test_truncated_or_empty_pcaps_never_pass_a_negative_check(tmp_path):
    # transfer run, header-only Ethernet pcap: eth_no_port cannot be judged
    code, rep = run(tmp_path, write_inputs(tmp_path, eth=pcap_bytes([])))
    assert code == chk.EXIT_INCONCLUSIVE and rep["checks"]["eth_no_port"]["unusable"] == ["pcap holds no packet"]
    # Ethernet pcap whose last record (a port-8081 SYN) is cut: inconclusive, not pass
    code, rep = run(tmp_path, write_inputs(tmp_path, eth=eth_pcap(with_port_packet=True)[:-10]))
    assert code == chk.EXIT_INCONCLUSIVE
    assert rep["checks"]["eth_no_port"] == {"status": "inconclusive", "port_packets": 0,
                                            "unusable": ["pcap file truncated"]}
    # control run whose Wi-Fi pcap ends in a cut port-8081 data record: inconclusive, not pass
    cut = QUIET + pcap_bytes([(T0 + 2, eth(PEN_IP, ipv4(PEN_IP, PHONE_IP, 6, tcp(40009, 8081, 5, 1, 0x18, b"data" * 20))))])[24:]
    code, rep = run(tmp_path, control_args(tmp_path, pcap=cut[:-30]))
    assert code == chk.EXIT_INCONCLUSIVE, rep["checks"]
    assert rep["checks"]["no_port_payload"]["status"] == "inconclusive"
    code, rep = run(tmp_path, control_args(tmp_path, pcap=cut))
    assert code == chk.EXIT_FAIL and rep["checks"]["no_port_payload"]["status"] == "fail"


def test_a_missing_tail_is_inconclusive_not_a_mismatch(tmp_path):
    """Review finding 9: the last client segment is missing but the phone ACKed it."""
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=build_session(drop_client_segment=6)))
    assert code == chk.EXIT_INCONCLUSIVE, rep["checks"]
    issues = rep["checks"]["tcp_reassembly_clean"]["issues"]
    tail = [i for i in issues if i["kind"] == "tail_missing"]
    assert tail and tail[0]["direction"] == "c2s" and tail[0]["missing_at_least"] > 0
    assert any(i["kind"] in ("trailing_partial_frame", "incomplete_message") for i in issues)
    assert rep["checks"]["pdu_out_match"]["status"] == "inconclusive"
    assert rep["checks"]["file_sha256"]["status"] == "inconclusive"


def test_a_truncated_pcap_file_turns_a_mismatch_into_inconclusive(tmp_path):
    changed = bytearray(HS_RSP)
    changed[-2] ^= 0x01
    cap = capture_json(out=[SAY_HELLO, bytes(changed), CONTENT_1, CONTENT_2])
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=build_session()[:-5], cap=cap))
    assert code == chk.EXIT_INCONCLUSIVE, rep["checks"]
    assert rep["checks"]["tcp_reassembly_clean"]["pcap_file_truncated"] is True
    assert rep["checks"]["pdu_out_match"]["status"] == "inconclusive"


def test_a_restarted_transfer_is_judged_on_the_finished_one(tmp_path):
    """Review finding 6: a FileSync restart leaves an unfinished transfer before the finished one."""
    partial = FileSyncContent(1700000000, 0, FILE[:300], False).encode()
    pcap = build_session(before_content=(partial,))
    cap = capture_json(out=[SAY_HELLO, HS_RSP, partial, CONTENT_1, CONTENT_2])
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=pcap, cap=cap))
    assert code == chk.EXIT_PASS, rep["checks"]
    check = rep["checks"]["file_sha256"]
    assert [t["sha256"] for t in check["finished"]] == [FILE_SHA]
    assert [(t["bytes"], t["finished"]) for t in check["unfinished_not_judged"]] == [(300, False)]


def test_an_answered_ping_counts_as_egress(tmp_path):
    """Review findings 7/15: ICMP echo replies from outside the user-mode network are egress;
    replies from slirp's own addresses are not (libslirp answers those itself)."""
    ping = [(PHONE_IP, ipv4(PHONE_IP, "8.8.8.8", 1, bytes([8, 0, 0, 0, 0, 1, 0, 1]) + b"ping")),
            ("8.8.8.8", ipv4("8.8.8.8", PHONE_IP, 1, bytes([0, 0, 0, 0, 0, 1, 0, 1]) + b"ping"))]
    gw = [(PHONE_IP, ipv4(PHONE_IP, "10.0.2.2", 1, bytes([8, 0, 0, 0, 0, 2, 0, 1]))),
          ("10.0.2.2", ipv4("10.0.2.2", PHONE_IP, 1, bytes([0, 0, 0, 0, 0, 2, 0, 1])))]
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=build_session(extra=gw)) + ["--expect-no-egress"])
    assert code == chk.EXIT_PASS and rep["egress"]["guest_icmp_answered"] == []
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=build_session(extra=ping)) + ["--expect-no-egress"])
    assert code == chk.EXIT_FAIL
    assert rep["checks"]["egress_none"]["answered"] == [{"from": PHONE_IP, "to": "8.8.8.8", "proto": "icmp"}]
    unanswered = ping[:1]
    code, rep = run(tmp_path, write_inputs(tmp_path, pcap=build_session(extra=unanswered)) + ["--expect-no-egress"])
    assert code == chk.EXIT_PASS


def test_file_sync_content_fields_follow_plaudsim_opt_int():
    """Review finding 10: the checker's coercion is org.json optInt, like plaudsim's."""
    from plaudsim.wifi import _opt_int, parse_pdu
    values = [0, 7, -3, 3.9, -2.5, float("inf"), float("nan"), 1e400, True, False, "3.0", "12", " 5 ", "x",
              "inf", "nan", None, [1], {"a": 1}]
    for v in values:
        assert chk.opt_int({"k": v}, "k", -1) == _opt_int({"k": v}, "k", -1), v
    assert chk.opt_int({}, "k", 9) == 9 and chk.opt_int(None, "k", 9) == 9
    cases = [(b'{"session":1,"offset":0,"length":1e400,"last":1}', b"x"),
             (b'{"session":1,"length":true,"last":true}', b"y"),
             (b'{"session":1,"length":"3.0","last":"1"}', b"abc"),
             (b'not json', b"z"),
             (b'[1,2]', b"w")]
    for body, tail in cases:
        raw = pack_pdu(13, body, tail)
        want = FileSyncContent.from_pdu(parse_pdu(raw))
        got = chk.file_transfers([raw])["transfers"][0]
        assert (got["bytes"], got["finished"], got["sha256"]) == (
            len(want.data), want.last, hashlib.sha256(want.data).hexdigest()), body


def test_an_internal_error_exits_2_with_a_report(tmp_path, monkeypatch):
    def boom(_args):
        raise RuntimeError("synthetic checker bug")
    monkeypatch.setattr(chk, "analyse", boom)
    (tmp_path / "wifi.pcap").write_bytes(QUIET)
    code = chk.main(["--wifi-pcap", str(tmp_path / "wifi.pcap"), "--report", str(tmp_path / "report.json")])
    rep = json.loads((tmp_path / "report.json").read_text())
    assert code == chk.EXIT_USAGE == rep["exit_code"]
    assert rep["verdict"] == "internal_error" and "synthetic checker bug" in rep["error"]


def test_reserved_control_opcodes_and_bad_close_frames_are_violations():
    """Review finding 11."""
    key = b"\x01\x02\x03\x04"
    frames, _t, _v = chk.parse_ws_frames(
        ws_frame(b"", opcode=0xB, mask=key) + ws_frame(b"\x03", opcode=8, mask=key)
        + ws_frame(struct.pack(">H", 1005), opcode=8, mask=key)
        + ws_frame(struct.pack(">H", 1000) + b"\xff\xfe", opcode=8, mask=key)
        + ws_frame(b"x", opcode=3, mask=key) + ws_frame(b"", opcode=0xF, mask=key)
        + ws_frame(struct.pack(">H", 4000) + b"ok", opcode=8, mask=key))
    _m, controls, viol, _i = chk.assemble_messages(frames, "out", 0, None)
    assert [v["kind"] for v in viol] == [
        "reserved control opcode 11", "close frame with a 1-byte body", "invalid close code 1005",
        "close reason is not UTF-8", "unknown opcode 3", "reserved control opcode 15"]
    assert controls[-1] == {"offset": controls[-1]["offset"], "opcode": "close", "len": 4,
                            "close_code": 4000, "close_reason": "ok"}
    assert [c for c in range(990, 5010) if chk.valid_close_code(c)] == (
        list(range(1000, 1004)) + list(range(1007, 1015)) + list(range(3000, 5000)))


def test_frame_lengths_masking_and_fragmentation():
    small, medium, large = b"s" * 5, bytes(range(256)) * 2, os.urandom(70000)
    stream = (ws_frame(small, mask=b"\x01\x02\x03\x04")
              + ws_frame(medium[:100], fin=False, mask=b"\x0a\x0b\x0c\x0d")
              + ws_frame(b"", opcode=10, mask=b"\x00\x00\x00\x01")
              + ws_frame(medium[100:], opcode=0, mask=b"\x0e\x0f\x10\x11")
              + ws_frame(large, mask=b"\xde\xad\xbe\xef")
              + ws_frame(b"\x03\xe8bye", opcode=8, mask=b"\x01\x01\x01\x01"))
    frames, trailing, violations = chk.parse_ws_frames(stream + b"\x82")   # plus one partial byte
    assert trailing == 1 and violations == []
    assert [f.length for f in frames] == [5, 100, 0, 412, 70000, 5]
    assert frames[1].header_len == 6 and frames[3].header_len == 8 and frames[4].header_len == 14
    msgs, controls, viol, incomplete = chk.assemble_messages(frames, "out", 0, None)
    assert viol == [] and incomplete is None
    assert [m.payload for m in msgs] == [small, medium, large]
    assert [c["opcode"] for c in controls] == ["pong", "close"] and controls[1]["close_code"] == 1000
    # the same frames seen as server->client violate the masking rule
    _msgs, _c, viol, _i = chk.assemble_messages(frames, "in", 0, None)
    assert {v["kind"] for v in viol} == {"server frame masked"}


def test_permessage_deflate_messages_are_inflated():
    params = chk.parse_deflate("permessage-deflate; client_max_window_bits=15; server_no_context_takeover")
    assert params is not None and params.server_no_context_takeover and not params.client_no_context_takeover
    comp = zlib.compressobj(wbits=-15)
    first = comp.compress(HS_REQ) + comp.flush(zlib.Z_SYNC_FLUSH)
    comp = zlib.compressobj(wbits=-15)                       # no context takeover on the server side
    second = comp.compress(FILE_SYNC_REQ) + comp.flush(zlib.Z_SYNC_FLUSH)
    assert first.endswith(b"\x00\x00\xff\xff") and second.endswith(b"\x00\x00\xff\xff")
    stream = ws_frame(first[:-4], rsv1=True) + ws_frame(second[:-4], rsv1=True) + ws_frame(b"plain")
    frames, _t, _v = chk.parse_ws_frames(stream)
    msgs, _c, viol, _i = chk.assemble_messages(frames, "in", 0, params)
    assert viol == [] and [m.payload for m in msgs] == [HS_REQ, FILE_SYNC_REQ, b"plain"]
    assert [m.compressed for m in msgs] == [True, True, False]
    # RSV1 without a negotiated extension is a protocol violation
    _m, _c, viol, _i = chk.assemble_messages(frames, "in", 0, None)
    assert any("reserved bits" in v["kind"] for v in viol)


def test_proc_net_tcp_rows_decode_v4_and_mapped_v6():
    rows = chk.parse_proc_net_tcp(PROC_ROWS + "   2: 1002000A:1F91 0202000A:A1B2 06 00000000:00000000\n")
    assert rows == [
        {"local": "::", "local_port": 8081, "remote": "::", "remote_port": 0, "state": "LISTEN"},
        {"local": "10.0.2.16", "local_port": 8081, "remote": "10.0.2.2", "remote_port": 0x9C41,
         "state": "ESTABLISHED"},
        {"local": "10.0.2.16", "local_port": 8081, "remote": "10.0.2.2", "remote_port": 0xA1B2,
         "state": "TIME_WAIT"}]
    assert chk.parse_logcat(LOGCAT) == ["10.0.2.2"]


def test_cli_exit_codes_and_input_errors(tmp_path):
    args = write_inputs(tmp_path)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    ok = subprocess.run([sys.executable, str(CHECKER_PATH), *args], capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.startswith("verdict=pass exit=0")
    assert json.loads((tmp_path / "report.json").read_text())["verdict"] == "pass"
    missing = subprocess.run([sys.executable, str(CHECKER_PATH), "--wifi-pcap", str(tmp_path / "nope.pcap")],
                             capture_output=True, text=True, env=env)
    assert missing.returncode == chk.EXIT_USAGE and "cannot read pcap" in missing.stderr
    (tmp_path / "ng.pcap").write_bytes(b"\x0a\x0d\x0d\x0a" + b"\0" * 28)
    assert chk.main(["--wifi-pcap", str(tmp_path / "ng.pcap")]) == chk.EXIT_USAGE
    (tmp_path / "bad.json").write_text("{not json")
    assert chk.main(["--wifi-pcap", str(tmp_path / "wifi.pcap"), "--capture-json",
                     str(tmp_path / "bad.json")]) == chk.EXIT_USAGE


def test_summary_mode_lists_connections_without_judging(tmp_path, capsys):
    (tmp_path / "wifi.pcap").write_bytes(build_session())
    assert chk.main(["--wifi-pcap", str(tmp_path / "wifi.pcap"), "--summary"]) == chk.EXIT_PASS
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] == "summary" and rep["checks"] == {}
    assert rep["port_summary"]["websocket_connections"] == 1


@pytest.mark.parametrize("magic", ["d4c3b2a1", "a1b2c3d4"])
def test_both_pcap_byte_orders_are_read(tmp_path, magic):
    frame = eth(PHONE_IP, ipv4(PHONE_IP, "10.0.2.3", 17, udp(1, 53, b"x")))
    endian = "<" if magic == "d4c3b2a1" else ">"
    data = (bytes.fromhex(magic) + struct.pack(endian + "HHiIII", 2, 4, 0, 0, 65535, 1)
            + struct.pack(endian + "IIII", 10, 500000, len(frame), len(frame)) + frame)
    (tmp_path / "x.pcap").write_bytes(data)
    pcap = chk.read_pcap(str(tmp_path / "x.pcap"))
    assert len(pcap.packets) == 1 and pcap.packets[0].ts == pytest.approx(10.5)
    d = chk.decode(pcap.packets[0], pcap.linktype)
    assert (d.proto, d.dport, d.payload) == ("udp", 53, b"x")
