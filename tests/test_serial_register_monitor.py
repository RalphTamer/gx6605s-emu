"""Execute the resident protocol against strict CPU/MMIO and pseudo-terminal fixtures."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pty
import select
import struct
import sys
import tempfile
import termios
import threading
import time
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import serial_register_monitor as monitor


class TargetBus(monitor.ram.DiagnosticBus):
    def __init__(self):
        super().__init__()
        self.target_reads = []
        self.target_widths = []

    def access(self, address, size, value=None, fetch=False):
        if not fetch and monitor.allowed(address, size):
            if value is not None:
                raise AssertionError("monitor wrote a target or used the wrong width")
            self.target_reads.append(address)
            self.target_widths.append(size)
            return (address ^ 0x12345678) & ((1 << (8 * size)) - 1)
        return super().access(address, size, value, fetch)


def boot():
    code, waiting = monitor.program()
    bus = TargetBus()
    bus.outgoing.extend(b"O")
    bus.map("monitor-ram", monitor.ram.ENTRY & 0x1fffffff, len(code), code)
    cpu = monitor.ram.CPU(bus, monitor.ram.ENTRY)
    cpu.cr[18] = 0x7d
    for _ in range(1000):
        if cpu.pc == waiting:
            break
        cpu.step()
    else:
        raise AssertionError("monitor did not announce ready")
    return bus, cpu, waiting


def feed(bus, cpu, waiting, packet):
    bus.incoming.extend(packet)
    before = len(bus.outgoing)
    cpu.step()
    for _ in range(10000):
        if cpu.pc == waiting:
            return bytes(bus.outgoing[before:])
        cpu.step()
    raise AssertionError("monitor did not return to its query loop")


def raw_request(address, transaction=17, marker=b"RREG"):
    body = marker + struct.pack("<II", address, transaction)
    return body + struct.pack("<I", zlib.crc32(body))


class ResidentMonitorTests(unittest.TestCase):
    def test_repeated_queries_execute_real_instructions_and_only_read_allowed_targets(self):
        bus, cpu, waiting = boot()
        self.assertIsNotNone(monitor.ready_reply(bytes(bus.outgoing)))
        addresses = [0x04e3ff44, 0x04600120, 0x04800180, 0x04c00000, 0x040036d4]
        for transaction, address in enumerate(addresses):
            reply = feed(bus, cpu, waiting, monitor.request(address, transaction))
            self.assertEqual(monitor.parse_response(reply, address, transaction), address ^ 0x12345678)
        self.assertEqual(bus.target_reads, addresses)
        self.assertTrue(all(address == 0x403000 for address, _, _ in bus.device_writes))
        self.assertFalse(bus.incoming)

    def test_guest_enforces_each_window_boundary_alignment_and_flash_exclusion(self):
        bus, cpu, waiting = boot()
        for start, end in monitor.WINDOWS:
            for address in (start, end - 4):
                reply = feed(bus, cpu, waiting, monitor.request(address, 19))
                self.assertEqual(monitor.parse_response(reply, address, 19), address ^ 0x12345678)
        before = list(bus.target_reads)
        for address in (0, 0x100000, 0x302000, 0xf80000, 0x10000000,
                        0x400000, 0x403000, 0x0030b000,
                        0x03fffffc, 0x05000000, 0x04e00001):
            reply = feed(bus, cpu, waiting, raw_request(address))
            with self.assertRaisesRegex(ValueError, "status=3"):
                monitor.parse_response(reply, address, 17)
            with self.assertRaises(ValueError):
                monitor.request(address, 17)
        self.assertEqual(bus.target_reads, before)

    def test_byte_halfword_and_word_commands_use_exact_hardware_width_and_bounds(self):
        bus, cpu, waiting = boot()
        for size in (1, 2, 4):
            for address in (0x04000000, 0x04f10004, 0x05000000 - size):
                reply = feed(bus, cpu, waiting, monitor.request(address, 29, size))
                self.assertEqual(monitor.parse_response(reply, address, 29, size),
                                 (address ^ 0x12345678) & ((1 << (8 * size)) - 1))
                self.assertEqual(bus.target_widths[-1], size)
            for address in (0x03ffffff, 0x05000000, 0x04ffffff if size != 1 else 0x05000001):
                reply = feed(bus, cpu, waiting, raw_request(address, marker=monitor.COMMANDS[size]))
                with self.assertRaisesRegex(ValueError, "status=3"):
                    monitor.parse_response(reply, address, 17, size)
        before = len(bus.target_reads)
        for marker in (b"WBYT", b"WHAL", b"WREG"):
            reply = feed(bus, cpu, waiting, raw_request(0x04f10004, marker=marker))
            with self.assertRaisesRegex(ValueError, "status=1"):
                monitor.parse_response(reply, 0x04f10004, 17)
        self.assertEqual(len(bus.target_reads), before)

    def test_bad_crc_or_write_like_command_never_reads_target(self):
        bus, cpu, waiting = boot()
        packet = bytearray(raw_request(0x04e3ff44))
        packet[-1] ^= 1
        reply = feed(bus, cpu, waiting, packet)
        with self.assertRaisesRegex(ValueError, "status=2"):
            monitor.parse_response(reply, 0x04e3ff44, 17)
        reply = feed(bus, cpu, waiting, raw_request(0x04e3ff44, marker=b"WREG"))
        with self.assertRaisesRegex(ValueError, "status=1"):
            monitor.parse_response(reply, 0x04e3ff44, 17)
        self.assertEqual(bus.target_reads, [])

    def test_partial_request_waits_without_reply_or_target_access(self):
        bus, cpu, waiting = boot()
        packet = monitor.request(0x04e3ff44, 9)
        initial = bytes(bus.outgoing)
        bus.incoming.extend(packet[:8])
        for _ in range(500):
            cpu.step()
        self.assertEqual(bytes(bus.outgoing), initial)
        self.assertEqual(bus.target_reads, [])
        reply = feed(bus, cpu, waiting, packet[8:])
        self.assertEqual(monitor.parse_response(reply, 0x04e3ff44, 9), 0x04e3ff44 ^ 0x12345678)

    def test_response_rejects_stale_transaction_wrong_address_and_corruption(self):
        bus, cpu, waiting = boot()
        reply = feed(bus, cpu, waiting, monitor.request(0x04600120, 7))
        for address, transaction in ((0x04600124, 7), (0x04600120, 8)):
            with self.assertRaises(ValueError):
                monitor.parse_response(reply, address, transaction)
        for offset in (0, 4, 8, 12, 16):
            broken = bytearray(reply)
            broken[offset] ^= 1
            with self.assertRaises(ValueError):
                monitor.parse_response(broken, 0x04600120, 7)

    def test_host_transport_logs_verified_queries_without_extra_writes_and_restores_settings(self):
        master, slave = pty.openpty()
        port = os.ttyname(slave)
        wire, extra = [], []

        def receiver():
            for _ in range(2):
                packet = bytearray()
                deadline = time.monotonic() + 3
                while len(packet) < 16 and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        packet.extend(os.read(master, 16 - len(packet)))
                if len(packet) != 16:
                    return
                wire.append(bytes(packet))
                marker, address, transaction, crc = struct.unpack("<4s3I", packet)
                if marker != b"RBYT" or zlib.crc32(packet[:12]) != crc:
                    return
                body = b"RVAL" + struct.pack("<III", address, transaction, (address ^ 0x12345678) & 255)
                reply = body + struct.pack("<I", zlib.crc32(body))
                os.write(master, reply[:7])
                os.write(master, reply[7:])
            if select.select([master], [], [], 0.1)[0]:
                extra.append(os.read(master, 4096))

        worker = threading.Thread(target=receiver)
        try:
            before = termios.tcgetattr(slave)
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                ready_path = Path(directory) / "ready.json"
                ready_path.write_text(json.dumps({"mode": monitor.MODE, "reason": "monitor-ready",
                    "port": port, "packet_sha256": monitor.hashlib.sha256(monitor.ram.wrap_stage2(monitor.program()[0])).hexdigest()}))
                ready_path.with_suffix(".bin").write_bytes(b"O" + monitor.MAGIC)
                prefix = Path(directory) / "queries"
                addresses = [0x04f10004, 0x04f10005]
                worker.start()
                report = monitor.read_registers(port, ready_path, addresses, prefix, size=1)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(report["reason"], "queries-complete")
                self.assertEqual(report["serial_bytes_sent"], 32)
                self.assertEqual(report["bytes_received"], 40)
                self.assertEqual(len(wire), 2)
                self.assertEqual(extra, [])
                self.assertEqual([r["value"] for r in report["records"]], ["0x%08x" % ((a ^ 0x12345678) & 255) for a in addresses])
                self.assertEqual([r["size"] for r in report["records"]], [1, 1])
                self.assertEqual(json.loads(prefix.with_suffix(".json").read_text()), report)
            after = termios.tcgetattr(slave)
            pending = getattr(termios, "PENDIN", 0)
            before[3] &= ~pending
            after[3] &= ~pending
            self.assertEqual(before, after)
        finally:
            if worker.ident is not None:
                worker.join(3)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
