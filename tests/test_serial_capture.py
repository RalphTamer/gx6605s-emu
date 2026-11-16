"""Physical serial capture tested against a pseudo-terminal, without a receiver."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import tempfile
import termios
import threading
import unittest


spec = importlib.util.spec_from_file_location("serial_capture", Path(__file__).resolve().parents[1] / "tools" / "serial_capture.py")
serial_capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serial_capture)


class SerialCaptureTests(unittest.TestCase):
    def test_preserves_input_without_echo_or_transmission_and_restores_settings(self):
        master, slave = pty.openpty()
        payload = b"RUN\r\nboot> \x00\xff\x1b[31m"
        ready, sent = threading.Event(), threading.Event()
        unexpected_output = []

        def sender():
            if not ready.wait(2):
                return
            os.write(master, payload)
            if select.select([master], [], [], 0.1)[0]:
                unexpected_output.append(os.read(master, 4096))
            sent.set()

        worker = threading.Thread(target=sender)
        try:
            before = termios.tcgetattr(slave)
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as shown:
                prefix = Path(directory) / "boot"
                worker.start()
                report = serial_capture.capture(os.ttyname(slave), 115200, 0.3, prefix, "synthetic", ready.set)
                worker.join(2)
                self.assertTrue(sent.is_set())
                self.assertEqual(Path(str(prefix) + ".bin").read_bytes(), payload)
                self.assertEqual(report["bytes_received"], len(payload))
                self.assertEqual(report["serial_bytes_sent"], 0)
                self.assertEqual(report["reason"], "duration-expired")
                self.assertEqual(json.loads(Path(str(prefix) + ".json").read_text()), report)
                self.assertEqual(unexpected_output, [])
                self.assertNotIn("\x1b", shown.getvalue())
                self.assertIn("\\x1b", shown.getvalue())
            after = termios.tcgetattr(slave)
            # BSD can set PENDIN when restoring canonical mode: queued input
            # must be reprocessed on the next read. It is runtime state, not a
            # changed setting; do not flush received input just to clear it.
            pending = getattr(termios, "PENDIN", 0)
            after[3] &= ~pending
            before[3] &= ~pending
            self.assertEqual(after, before)
        finally:
            ready.set()
            if worker.ident is not None:
                worker.join(2)
            os.close(master)
            os.close(slave)

    def test_existing_capture_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "keep"
            raw = Path(str(prefix) + ".bin")
            raw.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                serial_capture.capture("/no/device", 115200, 1, prefix)
            self.assertEqual(raw.read_bytes(), b"original")

    def test_boot_probe_sends_only_one_key_after_a_fragmented_marker(self):
        master, slave = pty.openpty()
        ready = threading.Event()
        received_keys = []

        def receiver():
            if not ready.wait(2):
                return
            os.write(master, b"Parti")
            # Confirm a partial marker alone cannot trigger transmission.
            if select.select([master], [], [], 0.05)[0]:
                received_keys.append(os.read(master, 4096))
            os.write(master, b"tion Version : 102\r\n")
            if select.select([master], [], [], 1)[0]:
                received_keys.append(os.read(master, 4096))
            os.write(master, b"boot> Partition Version")
            if select.select([master], [], [], 0.05)[0]:
                received_keys.append(os.read(master, 4096))

        worker = threading.Thread(target=receiver)
        try:
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                worker.start()
                report = serial_capture.capture(os.ttyname(slave), 115200, 0.3,
                    Path(directory) / "probe", on_ready=ready.set,
                    probe=serial_capture.BootKeyProbe("escape"))
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(received_keys, [b"\x1b"])
                self.assertEqual(report["serial_bytes_sent"], 1)
                self.assertEqual(len(report["tx_events"]), 1)
                self.assertEqual(report["tx_events"][0]["hex"], "1b")
                self.assertEqual(report["bytes_received"], len(b"Partition Version : 102\r\nboot> Partition Version"))
        finally:
            ready.set()
            if worker.ident is not None:
                worker.join(2)
            os.close(master)
            os.close(slave)

    def test_boot_probe_ignores_runtime_data_and_rejects_commands(self):
        probe = serial_capture.BootKeyProbe()
        self.assertEqual(probe.observe(b"1\0\0\0" * 10000), b"")
        self.assertLessEqual(len(probe.tail), len(probe.marker) - 1)
        with self.assertRaises(ValueError):
            serial_capture.BootKeyProbe("flash erase")


if __name__ == "__main__":
    unittest.main()
