"""Execute diagnostic machine code locally and exercise its serial continuation."""
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
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import serial_ram_diagnostic as diag


class RamDiagnosticTests(unittest.TestCase):
    def test_guest_reports_real_cpu_values_and_writes_only_uart_and_own_ram(self):
        code, idle = diag.diagnostic()
        bus = diag.DiagnosticBus()
        bus.map("diagnostic-ram", diag.ENTRY & 0x1fffffff, 4096, code)
        cpu = diag.CPU(bus, diag.ENTRY)
        cpu.cr[17], cpu.cr[18], cpu.cr[30] = 0x33, 0xfd, 0xc
        expected = {"cr0": "0x80000000", "cr17": "0x00000033",
                    "cr18": "0x000000fd", "cr30": "0x0000000c"}
        while cpu.steps < 10000 and cpu.pc != idle:
            cpu.step()
        self.assertEqual(cpu.pc, idle)
        self.assertEqual(diag.parse_reply(bus.outgoing), expected)
        self.assertEqual(len(bus.outgoing), diag.REPLY_SIZE)
        self.assertEqual(len(bus.device_writes), diag.REPLY_SIZE)
        self.assertTrue(all(address == 0x403000 and size == 4 for address, _, size in bus.device_writes))
        # A waiting transmitter must poll, without sending or advancing past it.
        cpu.pc = diag.ENTRY
        bus.outgoing.clear()
        original = bus.access
        bus.access = lambda address, size, value=None, fetch=False: 0 if address == 0x403014 and value is None else original(address, size, value, fetch)
        for _ in range(500):
            cpu.step()
        self.assertEqual(bus.outgoing, b"")

    def test_packet_layout_checksum_and_fragmented_reply(self):
        packet = diag.stage2_packet()
        checksum, size = struct.unpack_from("<II", packet)
        content = packet[8:]
        code, _ = diag.diagnostic()
        self.assertEqual(size, len(content))
        self.assertEqual(checksum, sum(content) & 0xffffffff)
        self.assertEqual(content[:8192 + 28], bytes(8192 + 28))
        self.assertEqual(struct.unpack_from("<I", content, 0x201c)[0], len(code))
        self.assertEqual(content[0x2020:], code)
        reply = b"O" + diag.MAGIC + struct.pack("<4I", 1, 2, 3, 4) + diag.FOOTER
        for end in range(len(reply)):
            self.assertIsNone(diag.parse_reply(reply[:end]))
        self.assertEqual(diag.parse_reply(reply)["cr18"], "0x00000003")
        with self.assertRaises(ValueError):
            diag.parse_reply(reply[:-1] + b"!")

    def test_serial_continuation_sends_only_fixed_packet_and_cannot_be_repeated(self):
        master, slave = pty.openpty()
        port = os.ttyname(slave)
        packet = diag.stage2_packet()
        received, extra = bytearray(), []
        reply = b"O" + diag.MAGIC + struct.pack("<4I", 1, 2, 3, 4) + diag.FOOTER

        def receiver():
            deadline = time.monotonic() + 3
            while len(received) < len(packet) and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    received.extend(os.read(master, 4096))
            os.write(master, reply[:4])
            os.write(master, reply[4:])
            if select.select([master], [], [], 0.1)[0]:
                extra.append(os.read(master, 4096))

        worker = threading.Thread(target=receiver)
        try:
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), patch.object(diag, "check_with_stage1", return_value={"synthetic_test": True}):
                ready_path = Path(directory) / "ready.json"
                ready_path.write_text(json.dumps({"reason": "stage1-ready", "port": port,
                                                 "loader_sha256": diag.LOADER_SHA256}))
                ready_path.with_suffix(".bin").write_bytes(b"\0XRUNGET")
                loader = Path(directory) / "loader"
                loader.write_bytes(b"fixture; offline check mocked only in host transport test")
                prefix = Path(directory) / "result"
                worker.start()
                report = diag.run(port, ready_path, loader, prefix)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(received, packet)
                self.assertEqual(extra, [])
                self.assertEqual(report["reason"], "diagnostic-executed")
                self.assertEqual(report["serial_bytes_sent"], len(packet))
                self.assertFalse(report["flash_commands_sent"])
                self.assertEqual(prefix.with_suffix(".bin").read_bytes(), reply)
                self.assertEqual(json.loads(prefix.with_suffix(".json").read_text()), report)
                with self.assertRaises(FileExistsError):
                    diag.run(port, ready_path, loader, Path(directory) / "repeat")
        finally:
            if worker.ident is not None:
                worker.join(3)
            os.close(master)
            os.close(slave)

    def test_unconfirmed_stage1_is_rejected_before_device_open(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready.json"
            ready.write_text(json.dumps({"reason": "runget-timeout"}))
            with self.assertRaises(ValueError):
                diag.run("/no/device", ready, "/no/loader", Path(directory) / "result")


if __name__ == "__main__":
    unittest.main()
