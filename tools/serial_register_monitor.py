#!/usr/bin/env python3
"""Resident RAM monitor with a read-only, bounded register-query protocol.

Only naturally aligned 8/16/32-bit reads in fixed SoC peripheral windows
are accepted. Query only specific firmware-accessed
registers: unimplemented addresses may stall and reads may have side effects.
It has no target-write, flash, memory-upload, or execute command.
Requests/replies include CRC32 and a host transaction ID. Power cycling exits
the monitor and restores normal flash startup. Post-bootstrap observations
do not establish native-boot reset values or complete device semantics.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import struct
import sys
import termios
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from csky_ram_asm import Assembler
from serial_capture import utc_now
from serial_flash_dump import crc_table
import serial_ram_diagnostic as ram
import serial_ram_probe as stage1

MAGIC = b"STAR-MON3".ljust(16, b"\0")
MODE = "read-only-resident-register-monitor"
WINDOWS = ((0x00203000, 0x00207000),  # I2C / CIR
           (0x0020a000, 0x0020c000),  # timers / watchdog
           (0x00305000, 0x00306000),  # GPIO
           (0x0030a000, 0x0030b000),  # GCTL
           (0x00500000, 0x00500400),  # interrupt controller
           (0x00900000, 0x00901000),  # EHCI
           (0x00a00000, 0x00a01000),  # OHCI0
           (0x00b00000, 0x00b01000),  # OHCI1
           (0x00c00000, 0x00c01000),  # DRAM controller
           (0x04000000, 0x05000000))  # AV peripherals
COMMANDS = {4: b"RREG", 2: b"RHAL", 1: b"RBYT"}


def allowed(address, size=4, windows=WINDOWS):
    return (type(address) is int and type(size) is int and size in COMMANDS and
            address % size == 0 and any(start <= address and address + size <= end for start, end in windows))


def program(base=ram.ENTRY, windows=WINDOWS):
    a = Assembler(base)
    e = a.emit
    e("lrw", 3, 0xa0403000)
    e("lrw", 10, "command-data")
    e("lrw", 11, "reply-data")
    e("lrw", 9, "crc-table")
    e("lrw", 4, "header")
    e("movi", 5, len(MAGIC))
    e("bsr", "send")
    a.label("command")
    e("mov", 4, 10)
    e("movi", 5, 16)
    e("bsr", "receive")
    for offset in (4, 8):
        e("load", 6, 10, offset, 4)
        e("store", 6, 11, offset, 4)
    e("load", 6, 10, 0, 4)
    for size, marker in COMMANDS.items():
        e("lrw", 7, int.from_bytes(marker, "little"))
        e("cmpne", 6, 7)
        e("bt", "command-%d-end" % size)
        e("movi", 13, size)
        e("br", "validate-crc")
        a.label("command-%d-end" % size)
    e("br", "bad-command")
    a.label("validate-crc")
    e("mov", 4, 10)
    e("movi", 5, 12)
    e("lrw", 8, 0xffffffff)
    e("bsr", "crc")
    e("load", 7, 10, 12, 4)
    e("cmpne", 8, 7)
    e("bt", "bad-crc")
    e("load", 8, 10, 4, 4)
    e("mov", 6, 8)
    e("mov", 7, 13)
    e("subi", 7, 1)
    e("and", 6, 7)
    e("cmpnei", 6, 0)
    e("bt", "bad-address")
    for index, (start, end) in enumerate(windows):
        e("lrw", 6, start)
        e("cmphs", 8, 6)
        e("bf", "window-%d-end" % index)
        e("lrw", 6, end)
        e("cmphs", 8, 6)
        e("bf", "read")
        a.label("window-%d-end" % index)
    e("br", "bad-address")
    a.label("read")
    e("lrw", 6, 0xa0000000)
    e("or", 8, 6)
    for size in (1, 2):
        e("cmpnei", 13, size)
        e("bt", "read-%d-end" % size)
        e("load", 12, 8, 0, size)
        e("br", "read-complete")
        a.label("read-%d-end" % size)
    e("load", 12, 8, 0, 4)
    a.label("read-complete")
    e("store", 12, 11, 12, 4)
    e("lrw", 6, int.from_bytes(b"RVAL", "little"))
    e("store", 6, 11, 0, 4)
    e("br", "response")
    for label, status in (("bad-command", 1), ("bad-crc", 2), ("bad-address", 3)):
        a.label(label)
        e("movi", 6, status)
        e("br", "error")
    a.label("error")
    e("store", 6, 11, 12, 4)
    e("lrw", 6, int.from_bytes(b"RERR", "little"))
    e("store", 6, 11, 0, 4)
    a.label("response")
    e("mov", 4, 11)
    e("movi", 5, 16)
    e("lrw", 8, 0xffffffff)
    e("bsr", "crc")
    e("store", 8, 11, 16, 4)
    e("mov", 4, 11)
    e("movi", 5, 20)
    e("bsr", "send")
    e("br", "command")
    a.label("crc")
    e("load", 6, 4, 0, 1)
    e("xor", 8, 6)
    e("mov", 7, 8)
    e("zextb", 7)
    e("lsli", 7, 2)
    e("addu", 7, 9)
    e("load", 7, 7, 0, 4)
    e("lsri", 8, 8)
    e("xor", 8, 7)
    e("addi", 4, 1)
    e("subi", 5, 1)
    e("cmpnei", 5, 0)
    e("bt", "crc")
    e("not", 8)
    e("jmp", 15)
    a.label("send")
    e("load", 6, 3, 20, 4)
    e("btsti", 6, 6)
    e("bf", "send")
    e("load", 2, 4, 0, 1)
    e("store", 2, 3, 0, 4)
    e("addi", 4, 1)
    e("subi", 5, 1)
    e("cmpnei", 5, 0)
    e("bt", "send")
    e("jmp", 15)
    a.label("receive")
    e("load", 6, 3, 20, 4)
    e("btsti", 6, 0)
    e("bf", "receive")
    e("load", 2, 3, 0, 4)
    e("store", 2, 4, 0, 1)
    e("addi", 4, 1)
    e("subi", 5, 1)
    e("cmpnei", 5, 0)
    e("bt", "receive")
    e("jmp", 15)
    code = a.finish({"header": MAGIC, "command-data": bytes(16),
                     "reply-data": bytes(20), "crc-table": crc_table()})
    return code, a.labels["command"]


def request(address, transaction, size=4, windows=WINDOWS):
    if not allowed(address, size, windows) or type(transaction) is not int or not 0 <= transaction <= 0xffffffff:
        raise ValueError("request requires an allowed aligned register and a 32-bit transaction ID")
    body = COMMANDS[size] + struct.pack("<II", address, transaction)
    return body + struct.pack("<I", zlib.crc32(body))


def parse_response(data, address, transaction, size=4):
    if len(data) != 20:
        raise ValueError("register response must contain exactly 20 bytes")
    marker, echoed_address, echoed_transaction, value, crc = struct.unpack("<4s4I", data)
    if zlib.crc32(data[:16]) != crc or echoed_address != address or echoed_transaction != transaction:
        raise ValueError("register response CRC/address/transaction mismatch")
    if marker == b"RERR":
        raise ValueError("resident monitor rejected the request: status=%d" % value)
    if marker != b"RVAL":
        raise ValueError("unknown register response")
    if type(size) is not int or size not in COMMANDS or value >= 1 << (8 * size):
        raise ValueError("register response value exceeds requested width")
    return value


def ready_reply(data):
    if len(data) < 1 + len(MAGIC):
        return None
    if data != b"O" + MAGIC:
        raise ValueError("resident monitor startup frame invalid")
    return {"read_only": True, "windows": [[hex(start), hex(end)] for start, end in WINDOWS],
            "state": "waiting for CRC-checked register queries in RAM"}


def check_monitor(loader):
    code, waiting = program()
    bus = ram.DiagnosticBus()
    cpu = ram.simulate_continuation(loader, code, waiting, bus)
    if ready_reply(bytes(bus.outgoing)) is None:
        raise ValueError("offline resident monitor failed to announce ready")
    return {"steps": cpu.steps, "ready_frame_verified": True,
            "limitations": "post-RUNGET modeled UART/RAM; unit tests exercise query validation; physical MMIO semantics are not assumed"}


def read_registers(port, monitor_report, addresses, prefix, size=4):
    monitor_report, prefix = Path(monitor_report), Path(prefix)
    ready = json.loads(monitor_report.read_text())
    expected_hash = hashlib.sha256(ram.wrap_stage2(program()[0])).hexdigest()
    startup = monitor_report.with_suffix('.bin').read_bytes()
    windows = WINDOWS
    valid = (ready.get('mode') == MODE and ready.get('reason') == 'monitor-ready' and
             ready.get('packet_sha256') == expected_hash and startup == b'O' + MAGIC)
    if ready.get('mode') == 'fixed-av-startup-with-resident-reader':
        import serial_av_startup_probe as av
        valid = av.validate_monitor_report(ready, startup)
        windows = av.WINDOWS
    if not valid or ready.get('port') != port:
        raise ValueError("requires the matching successful resident-monitor report and startup capture")
    if not 1 <= len(addresses) <= 512 or any(not allowed(address, size, windows) for address in addresses):
        raise ValueError('query list must contain 1..512 allowed aligned registers')
    raw_path, report_path = Path(str(prefix) + ".bin"), Path(str(prefix) + ".json")
    if any(p.exists() or p.is_symlink() for p in (raw_path, report_path)):
        raise FileExistsError("query output already exists")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report = {"mode": "resident-read-only-register-queries", "port": port,
              "monitor_report": str(monitor_report), "started_utc": utc_now(),
              "reason": "error", "serial_bytes_sent": 0, "bytes_received": 0,
              "requests": [], "records": [], "target_registers_written": False,
              "flash_commands_sent": False,
              "measurement_context": "after pinned RAM bootstrap; sequential resident-monitor reads, not native-kernel read boundaries"}
    with ExitStack() as stack:
        raw = stack.enter_context(raw_path.open("xb"))
        metadata = stack.enter_context(report_path.open("x"))
        fd, old = None, None
        try:
            fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
            old = termios.tcgetattr(fd)
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL)
            for flag in ("CRTSCTS", "CCTS_OFLOW", "CRTS_IFLOW"):
                attrs[2] &= ~getattr(termios, flag, 0)
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            if select.select([fd], [], [], 0.1)[0]:
                pending = os.read(fd, 4096)
                if pending:
                    raw.write(pending)
                    report["bytes_received"] += len(pending)
                    raise ValueError("unexpected pending device output; monitor state may have changed")
            for address in addresses:
                transaction = secrets.randbits(32)
                packet = request(address, transaction, size, windows)
                event = {"utc": utc_now(), "address": hex(address), "size": size, "transaction": transaction,
                         "request_hex": packet.hex(), "written_bytes": 0}
                report["requests"].append(event)
                deadline = time.monotonic() + 5
                while event["written_bytes"] < len(packet):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                        raise TimeoutError("resident monitor query write timed out")
                    try:
                        n = os.write(fd, packet[event["written_bytes"]:])
                    except BlockingIOError:
                        continue
                    if n <= 0:
                        raise OSError("register query write made no progress")
                    event["written_bytes"] += n
                    report["serial_bytes_sent"] += n
                reply = bytearray()
                deadline = time.monotonic() + 5
                while len(reply) < 20:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                        raise TimeoutError("register 0x%08x did not return a complete reply" % address)
                    try:
                        block = os.read(fd, 20 - len(reply))
                    except BlockingIOError:
                        continue
                    if not block:
                        raise OSError("serial device disconnected")
                    raw.write(block)
                    raw.flush()
                    reply.extend(block)
                    report["bytes_received"] += len(block)
                value = parse_response(reply, address, transaction, size)
                report["records"].append({"address": "0x%08x" % address,
                                          "size": size,
                                          "value": "0x%08x" % value,
                                          "transaction": transaction, "crc32_verified": True})
            report["reason"] = "queries-complete"
        except KeyboardInterrupt:
            report["reason"] = "interrupted"
        except Exception as exc:
            report.update(reason="error", error=str(exc))
            raise
        finally:
            if fd is not None:
                try:
                    if old is not None:
                        termios.tcsetattr(fd, termios.TCSANOW, old)
                except termios.error as exc:
                    report["restore_error"] = str(exc)
                finally:
                    os.close(fd)
            report["ended_utc"] = utc_now()
            json.dump(report, metadata, indent=2)
            metadata.write("\n")
    print("%d verified register replies. Report: %s" % (len(report["records"]), report_path), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="receiver must already be off; arm then connect power")
    load.add_argument("--loader", type=Path, required=True)
    load.add_argument("--seconds", type=float, default=600)
    read = sub.add_parser("read", help="query an already loaded monitor without power cycling")
    read.add_argument("--monitor-report", type=Path, required=True)
    read.add_argument("--address", action="append", type=lambda s: int(s, 0))
    read.add_argument("--start", type=lambda s: int(s, 0))
    read.add_argument("--count", type=int, default=1)
    read.add_argument("--size", type=int, choices=(1, 2, 4), default=4, help="target read width in bytes")
    for child in (load, read):
        child.add_argument("--port", required=True)
        child.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-register-monitor-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    try:
        if args.command == "read":
            if args.address and args.start is not None:
                raise ValueError("select --address or --start, not both")
            if args.address:
                addresses = args.address
            elif args.start is not None and 1 <= args.count <= 512:
                addresses = [args.start + args.size * n for n in range(args.count)]
            else:
                raise ValueError("provide --address or --start with --count in 1..512")
            report = read_registers(args.port, args.monitor_report, addresses, args.output, args.size)
            for record in report["records"]:
                print(record["address"], record["value"], flush=True)
            return 0 if report["reason"] == "queries-complete" else 3
        print("Checking read-only resident monitor locally...", flush=True)
        offline = check_monitor(args.loader.read_bytes())
        boot_prefix = Path(str(args.output) + "-stage1")
        boot = stage1.probe(args.port, args.loader, args.seconds, boot_prefix)
        if boot["reason"] != "stage1-ready":
            return 3
        report = ram.run(args.port, Path(str(boot_prefix) + ".json"), args.loader, args.output,
                         packet_factory=lambda: ram.wrap_stage2(program()[0]),
                         verifier=lambda loader: offline if hashlib.sha256(loader).hexdigest() == stage1.LOADER_SHA256 else check_monitor(loader),
                         reply_parser=ready_reply, mode=MODE, seconds=20,
                         result_key="monitor", success_reason="monitor-ready")
        if report["reason"] != "monitor-ready":
            return 3
        samples = read_registers(args.port, Path(str(args.output) + ".json"),
                                 [0x04e3ff44, 0x04e3ffc0, 0x04600120], Path(str(args.output) + "-samples"))
        for record in samples["records"]:
            print(record["address"], record["value"], flush=True)
        print("Monitor remains in RAM, ready for further read-only queries. Keep receiver powered on.", flush=True)
        return 0
    except (OSError, ValueError, termios.error) as exc:
        print("Resident monitor failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
