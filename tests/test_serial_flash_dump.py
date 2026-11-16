"""Execute the read-only RAM reader and reject damaged/truncated flash frames."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import serial_flash_dump as flash


def execute(image, jedec=flash.JEDEC, stalled=False):
    code, idle = flash.reader(len(image), poll_budget=2 if stalled else 1 << 20)
    bus = flash.ReaderBus(image, jedec, stalled)
    bus.outgoing.extend(b"O")  # stage-1 checksum acknowledgment
    bus.map("flash-reader-code", flash.ram.ENTRY & 0x1fffffff, len(code), code)
    bus.map("flash-copy-ram", 0x10000000, len(image))
    cpu = flash.CPU(bus, flash.ram.ENTRY)
    cpu.cr[18] = 0x7d
    while cpu.steps < 300000 and cpu.pc != idle:
        cpu.step()
    if cpu.pc != idle:
        raise AssertionError("reader did not terminate within its instruction budget")
    return bus


class FlashReaderTests(unittest.TestCase):
    def test_reads_exact_bytes_and_crc_without_flash_mutation(self):
        image = bytes(range(256)) * 16
        bus = execute(image)
        info = flash.parse_frame(bus.outgoing, len(image))
        self.assertEqual(info["crc32"], "%08x" % zlib.crc32(image))
        self.assertEqual(info["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual(bus.outgoing[1 + flash.HEADER_SIZE:-len(flash.FOOTER)], image)
        self.assertEqual(bus.spi.flash.data[:len(image)], image)
        self.assertEqual(bus.spi.flash.read_bytes, len(image))
        self.assertEqual(bus.commands, [0x9f, 0x03])
        self.assertFalse(bus.spi.flash.selected)
        self.assertTrue(all(address in (0x403000, 0x302000, 0x302008) for address, _, _ in bus.device_writes))

    def test_unexpected_identity_never_reads_flash_data(self):
        bus = execute(bytes(32), jedec=0xef4016)
        self.assertEqual(bus.commands, [0x9f])
        self.assertEqual(bus.spi.flash.read_bytes, 0)
        self.assertFalse(bus.spi.flash.selected)
        self.assertEqual(len(bus.outgoing), 1 + flash.HEADER_SIZE + len(flash.FOOTER))
        with self.assertRaisesRegex(ValueError, "status=2, JEDEC=ef4016"):
            flash.parse_frame(bus.outgoing, 32)

    def test_controller_timeout_deselects_flash_and_reports_failure(self):
        bus = execute(bytes(32), stalled=True)
        self.assertEqual(bus.commands, [0x9f])
        self.assertFalse(bus.spi.flash.selected)
        with self.assertRaisesRegex(ValueError, "status=1"):
            flash.parse_frame(bus.outgoing, 32)

    def test_incomplete_frame_is_not_success_and_corruption_is_rejected(self):
        image = bytes(range(64))
        frame = bytes(execute(image).outgoing)
        for n in range(len(frame)):
            self.assertIsNone(flash.parse_frame(frame[:n], len(image)))
        for offset in (1, 17, 25, 1 + flash.HEADER_SIZE, len(frame) - 1):
            damaged = bytearray(frame)
            damaged[offset] ^= 1
            with self.assertRaises(ValueError):
                flash.parse_frame(damaged, len(image))
        with self.assertRaises(ValueError):
            flash.parse_frame(frame + b"extra", len(image))

    def test_stage2_lengths_match_generated_reader_and_read_range_is_bounded(self):
        for size in (4, 4096, flash.SIZE):
            code, _ = flash.reader(size)
            packet = flash.ram.wrap_stage2(code)
            checksum, length = struct.unpack_from("<II", packet)
            self.assertEqual(checksum, sum(packet[8:]) & 0xffffffff)
            self.assertEqual(length, len(packet) - 8)
            self.assertEqual(packet[8 + 0x2020:], code)
        for size in (-4, 0, 3, flash.SIZE + 4, True):
            with self.assertRaises(ValueError):
                flash.reader(size)


if __name__ == "__main__":
    unittest.main()
