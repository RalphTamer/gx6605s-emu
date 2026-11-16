#!/usr/bin/env python3
"""Fixed post-RAM-bootstrap register observations; no target-register writes.

The default preset reads configuration 040036D4 and VPU 04800140..048001BC;
the demod-id preset reads the fixed 04E3FFC0..04E3FFFC window. Each record
is transmitted separately with its address and guest-calculated CRC32. A
partial capture preserves completed records if a later bus read cannot finish.
These are post-bootstrap observations, not measured power-on reset values or
values at the original firmware's decoder boundary. No flash command is sent.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from csky_ram_asm import Assembler
import serial_ram_diagnostic as ram
import serial_ram_probe as stage1
from serial_flash_dump import crc_table

ADDRESSES = (0x040036d4, *range(0x04800140, 0x048001c0, 4))
GROUPS = {"boot-and-vpu": ADDRESSES,
          "demod-id": tuple(range(0x04e3ffc0, 0x04e40000, 4)),
          "kernel-reads": (0x04e3ffc0, 0x04600120, *range(0x04e3ffc4, 0x04e40000, 4))}
MAGIC = b"STAR-REG1".ljust(16, b"\0")
FOOTER = b"END-REG1"
RECORD_SIZE = 20


def reader(addresses=ADDRESSES):
    if addresses not in GROUPS.values():
        raise ValueError("register probe supports only fixed reviewed groups")
    a = Assembler(ram.ENTRY)
    e = a.emit
    e("lrw", 3, 0xa0403000)
    e("lrw", 4, "header")
    e("movi", 5, len(MAGIC))
    e("bsr", "send")
    e("lrw", 10, "records")
    e("lrw", 11, "footer")
    e("lrw", 9, "crc-table")
    a.label("read")
    # Transmit the next address BEFORE the read. An unfinished bus read then
    # leaves its address in the capture without losing previous valid records.
    e("mov", 4, 10)
    e("movi", 5, 8)
    e("bsr", "send")
    e("load", 7, 10, 4, 4)
    e("lrw", 6, 0xa0000000)
    e("or", 7, 6)
    e("load", 8, 7, 0, 4)
    e("store", 8, 10, 8, 4)
    e("mov", 4, 10)
    e("addi", 4, 4)
    e("movi", 5, 8)
    e("lrw", 8, 0xffffffff)
    e("bsr", "crc")
    e("store", 8, 10, 12, 4)
    e("mov", 4, 10)
    e("addi", 4, 8)
    e("movi", 5, 12)
    e("bsr", "send")
    e("addi", 10, RECORD_SIZE)
    e("cmpne", 10, 11)
    e("bt", "read")
    e("lrw", 4, "footer")
    e("movi", 5, len(FOOTER))
    e("bsr", "send")
    a.label("idle")
    e("br", "idle")
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
    e("movi", 7, 64)
    e("and", 6, 7)
    e("cmpnei", 6, 0)
    e("bf", "send")
    e("load", 2, 4, 0, 1)
    e("store", 2, 3, 0, 4)
    e("addi", 4, 1)
    e("subi", 5, 1)
    e("cmpnei", 5, 0)
    e("bt", "send")
    e("jmp", 15)
    records = b"".join(b"REG1" + struct.pack("<III", address, 0, 0) + b"END1" for address in addresses)
    code = a.finish({"header": MAGIC, "records": records, "footer": FOOTER, "crc-table": crc_table()})
    return code, a.labels["idle"]


def observations(data, addresses=ADDRESSES):
    """Parse completed CRC-checked records; never invent a missing read value."""
    result = {"values": {}, "pending_address": None, "complete": False,
              "measurement_context": "after pinned RAM bootstrap; target registers not written by this probe"}
    if len(data) < 1 + len(MAGIC):
        return result
    if data[:1] != b"O" or data[1:17] != MAGIC:
        raise ValueError("invalid register-probe acknowledgment or header")
    for n, expected_address in enumerate(addresses):
        offset = 17 + n * RECORD_SIZE
        if len(data) < offset + 8:
            return result
        if data[offset:offset + 4] != b"REG1":
            raise ValueError("invalid register record marker")
        address = struct.unpack_from("<I", data, offset + 4)[0]
        if address != expected_address:
            raise ValueError("register address differs from fixed probe order")
        result["pending_address"] = "0x%08x" % address
        if len(data) < offset + RECORD_SIZE:
            return result
        value, crc = struct.unpack_from("<II", data, offset + 8)
        if data[offset + 16:offset + 20] != b"END1" or zlib.crc32(data[offset + 4:offset + 12]) != crc:
            raise ValueError("register record CRC32 or footer mismatch")
        result["values"]["0x%08x" % address] = "0x%08x" % value
        result["pending_address"] = None
    total = 17 + RECORD_SIZE * len(addresses) + len(FOOTER)
    if len(data) < total:
        return result
    if len(data) != total or data[-len(FOOTER):] != FOOTER:
        raise ValueError("invalid register-probe final length/footer")
    result["complete"] = True
    return result


def parse_reply(data, addresses=ADDRESSES):
    result = observations(data, addresses)
    return result if result["complete"] else None


class RegisterBus(ram.DiagnosticBus):
    """Offline fixture allowing exactly one word read of each fixed target."""
    def __init__(self, values):
        super().__init__()
        self.values, self.target_reads = dict(values), []

    def access(self, address, size, value=None, fetch=False):
        if not fetch and address in self.values:
            if size != 4 or value is not None:
                raise ValueError("register probe attempted a target write or non-word read")
            self.target_reads.append(address)
            return self.values[address]
        return super().access(address, size, value, fetch)


def check_probe(loader, addresses=ADDRESSES):
    values = {address: (0x12345678 ^ address) for address in addresses}
    code, idle = reader(addresses)
    bus = RegisterBus(values)
    cpu = ram.simulate_continuation(loader, code, idle, bus)
    result = parse_reply(bus.outgoing, addresses)
    expected = {"0x%08x" % address: "0x%08x" % value for address, value in values.items()}
    if result is None or result["values"] != expected or bus.target_reads != list(addresses):
        raise ValueError("offline register observations differ from fixture")
    if any(address not in (0x403000, 0xf80004) for address, _, _ in bus.device_writes):
        raise ValueError("offline probe made an unexpected peripheral write")
    return {"steps": cpu.steps, "targets": len(addresses), "crc_records_verified": True,
            "target_writes": 0, "limitations": "modeled target values; physical read completion/values not assumed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--loader", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--group", choices=tuple(GROUPS), default="boot-and-vpu")
    parser.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-register-probe-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    addresses = GROUPS[args.group]
    capture_path = Path(str(args.output) + ".bin")
    try:
        print("Checking fixed read-only register probe locally...", flush=True)
        offline = check_probe(args.loader.read_bytes(), addresses)
        boot_prefix = Path(str(args.output) + "-stage1")
        boot = stage1.probe(args.port, args.loader, args.seconds, boot_prefix)
        if boot["reason"] != "stage1-ready":
            return 3
        report = ram.run(args.port, Path(str(boot_prefix) + ".json"), args.loader, args.output,
                         packet_factory=lambda: ram.wrap_stage2(reader(addresses)[0]),
                         verifier=lambda loader: offline if hashlib.sha256(loader).hexdigest() == stage1.LOADER_SHA256 else check_probe(loader, addresses),
                         reply_parser=lambda data: parse_reply(data, addresses), mode="fixed-read-only-register-probe-" + args.group, seconds=20,
                         result_key="register_observations", success_reason="registers-observed")
        return 0 if report["reason"] == "registers-observed" else 3
    except (OSError, ValueError, ram.termios.error) as exc:
        print("Register probe failed: %s" % exc, file=sys.stderr)
        return 2
    finally:
        if capture_path.exists():
            # Preserve useful completed records even if a later MMIO read hung.
            try:
                result = observations(capture_path.read_bytes(), addresses)
                result["group"] = args.group
                observed_path = Path(str(args.output) + ".observations.json")
                with observed_path.open("x") as out:
                    json.dump(result, out, indent=2)
                    out.write("\n")
                print("Completed register records: %d; unfinished read: %s" % (len(result["values"]), result["pending_address"]), flush=True)
            except (OSError, ValueError) as exc:
                print("Register observations not accepted: %s" % exc, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
