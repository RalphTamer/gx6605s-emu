"""Boundaries of the GX6605S stage-1 probe, using synthetic serial input."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pty
import select
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("serial_ram_probe", TOOLS / "serial_ram_probe.py")
ram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ram)


class RamProbeTests(unittest.TestCase):
    def test_wire_transfer_stops_at_runget_without_sending_stage2(self):
        fixture = ram.HEADER + bytes(ram.LOADER_SIZE - len(ram.HEADER))
        digest = ram.hashlib.sha256(fixture).hexdigest()
        master, slave = pty.openpty()
        ready = threading.Event()
        wire = bytearray()
        extra = []

        def receiver():
            if not ready.wait(2):
                return
            os.write(master, b"\0")
            if select.select([master], [], [], 0.05)[0]:
                extra.append(os.read(master, 4096))
            os.write(master, b"X")
            deadline = time.monotonic() + 2
            while len(wire) < 8197 and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    wire.extend(os.read(master, 4096))
            os.write(master, b"RUN")
            os.write(master, b"GET")
            if select.select([master], [], [], 0.1)[0]:
                extra.append(os.read(master, 4096))

        worker = threading.Thread(target=receiver)
        try:
            before = termios.tcgetattr(slave)
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), patch.object(ram, "LOADER_SHA256", digest):
                loader = Path(directory) / "fixture.boot"
                loader.write_bytes(fixture)
                prefix = Path(directory) / "probe"
                worker.start()
                report = ram.probe(os.ttyname(slave), loader, 3, prefix, ready.set)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(bytes(wire), ram.stage1_packet(fixture))
                self.assertEqual(extra, [])
                self.assertEqual(report["reason"], "stage1-ready")
                self.assertEqual(report["serial_bytes_sent"], 8197)
                self.assertFalse(report["stage2_sent"])
                self.assertFalse(report["flash_commands_sent"])
                self.assertEqual(Path(str(prefix) + ".bin").read_bytes(), b"\0XRUNGET")
                self.assertEqual(json.loads(Path(str(prefix) + ".json").read_text()), report)
            after = termios.tcgetattr(slave)
            pending = getattr(termios, "PENDIN", 0)
            before[3] &= ~pending
            after[3] &= ~pending
            self.assertEqual(after, before)
        finally:
            ready.set()
            if worker.ident is not None:
                worker.join(3)
            os.close(master)
            os.close(slave)

    def test_unreviewed_loader_is_rejected(self):
        with self.assertRaises(ValueError):
            ram.stage1_packet(bytes(ram.LOADER_SIZE))
        with self.assertRaises(ValueError):
            ram.stage1_packet(b"toob")

    def test_stage1_packet_excludes_header_and_stops_before_stage2(self):
        fixture = ram.HEADER + bytes(ram.LOADER_SIZE - len(ram.HEADER))
        digest = ram.hashlib.sha256(fixture).hexdigest()
        with patch.object(ram, "LOADER_SHA256", digest):
            packet = ram.stage1_packet(fixture)
        self.assertEqual(len(packet), 8197)
        self.assertEqual(packet[:5], bytes.fromhex("5900080000"))
        self.assertEqual(packet[5:-4], fixture[0x20:0x201c])
        self.assertEqual(packet[-4:], b"boot")

    def test_runtime_and_ascii_x_never_trigger_but_fragmented_rom_prefix_does(self):
        h = ram.Handshake()
        self.assertFalse(h.observe(b"1\0\0\0" * 10000))
        self.assertFalse(h.observe(b"CSKY X"))
        self.assertFalse(h.observe(b"\0"))
        self.assertTrue(h.observe(b"X"))
        self.assertFalse(h.observe(b"\0X"))
        self.assertEqual(h.state, "waiting-for-runget")
        self.assertFalse(h.observe(b"RUN"))
        self.assertFalse(h.observe(b"GET"))
        self.assertEqual(h.state, "stage1-ready")
        self.assertLessEqual(len(h.tail), 64)

    def test_late_handshake_never_uploads_and_normal_boot_is_not_success(self):
        h = ram.Handshake()
        self.assertFalse(h.observe(b"\0XPartition Version"))
        self.assertEqual(h.state, "missed-rom-window")
        self.assertFalse(h.observe(b"\0X"))
        h = ram.Handshake()
        self.assertTrue(h.observe(b"\0X"))
        self.assertFalse(h.observe(b"Partition Version"))
        self.assertEqual(h.state, "flash-startup-continued")


if __name__ == "__main__":
    unittest.main()
