"""GX6605S timer register sequences and deterministic counter behavior."""
import json
from pathlib import Path
import tempfile
import unittest

from starsat_emu.firmware import Firmware
from starsat_emu.memory import EmulationStop
from starsat_emu.profile import MachineProfile
from starsat_emu.timer import Timers


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.time = 0
        self.timer = Timers(lambda: self.time, input_hz=1000000)

    def write(self, offset, value):
        return self.timer.access(offset, 4, value)

    def read(self, offset):
        return self.timer.access(offset, 4)

    def test_reset_enable_start_pause_and_fractional_divider(self):
        self.write(0x24, 29)  # 1/3 count per virtual instruction
        self.write(0x10, 1)
        self.write(0x20, 1)
        self.write(0x10, 2)
        for now, value in [(1, 0), (2, 0), (3, 1), (9, 3)]:
            self.time = now
            self.assertEqual(self.read(4), value)
        self.write(0x20, 0)
        self.time = 99
        self.assertEqual(self.read(4), 3)
        self.write(0x20, 1)
        self.time = 102
        self.assertEqual(self.read(4), 4)
        self.write(0x10, 0)
        self.time = 1002
        self.assertEqual(self.read(4), 4)
        self.write(0x28, 123)
        self.write(0x10, 1)
        self.assertEqual(self.read(4), 123)

    def test_linux_event_sequence_overflow_irq_pending_and_w1c(self):
        self.write(0x24, 9)  # 1 count per instruction
        self.write(0x10, 1)
        self.write(0x20, 3)
        self.write(0x28, 0xfffffffe)
        self.write(0x10, 2)
        self.time = 3
        self.assertEqual(self.read(4), 0xffffffff)
        self.assertEqual(self.read(0), 1)
        self.assertTrue(self.timer.snapshot()["channels"][0]["irq_pending"])
        self.write(0, 0)
        self.assertEqual(self.read(0), 1)
        self.write(0, 2)
        self.assertEqual(self.read(0), 1)
        self.write(0, 3)
        self.assertEqual(self.read(0), 0)
        self.assertFalse(self.timer.snapshot()["channels"][0]["irq_pending"])
        self.time = 4
        self.assertEqual(self.read(4), 0xfffffffe)
        self.assertEqual(self.read(0), 1)
        # Linux's IRQ path clears the flag and writes reload=0 to avoid
        # another short-period event until set_next_event programs it.
        self.write(0, 1)
        self.write(0x28, 0)
        self.time = 5
        self.assertEqual(self.read(4), 1)
        self.assertEqual(self.read(0), 0)

    def test_two_channels_have_independent_state(self):
        self.write(0x24, 9)
        self.write(0x20, 1)
        self.write(0x10, 2)
        self.write(0x64, 19)
        self.write(0x68, 100)
        self.write(0x60, 1)
        self.write(0x50, 2)
        self.time = 10
        self.assertEqual(self.read(4), 10)
        self.assertEqual(self.read(0x44), 105)
        self.write(0x10, 0)
        self.time = 20
        self.assertEqual(self.read(4), 10)
        self.assertEqual(self.read(0x44), 110)

    def test_reserved_accesses_and_counter_writes_stop(self):
        for offset, size, value in [(8, 4, None), (0x80, 4, None), (4, 1, None),
                                    (4, 4, 1), (0, 4, 4), (0x10, 4, 4), (0x20, 4, 4)]:
            with self.assertRaises(EmulationStop):
                self.timer.access(offset, size, value)


class ObservedProfileTests(unittest.TestCase):
    def test_profile_opt_in_register_values_and_flash_identity(self):
        from starsat_emu.session import Session
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.bin"
            path.write_bytes(bytes.fromhex("aa55aa55") + bytes(0x3000))
            firmware = Firmware(path)
            self.assertEqual(firmware.offset, 0)
            s = Session(firmware, MachineProfile.load("receiver-observed"))
            self.assertIsNotNone(s.cpu.bus.timers)
            self.assertEqual(s.cpu.bus.spi.flash.jedec, bytes.fromhex("c84016"))
            self.assertEqual(s.cpu.bus.read(0x040036d4), 0)
            self.assertEqual(s.cpu.bus.read(0x04800180), 0x32110101)
            self.assertEqual(s.cpu.bus.read(0x04e3ffc0), 0x04040404)
            self.assertEqual(s.cpu.bus.read(0x04e3ffc4), 0x4c4c4c4c)
            self.assertEqual(s.cpu.bus.read(0x04e3fe44), 0x93939393)
            self.assertEqual(s.cpu.bus.read(0x04e3ff44), 0)
            self.assertEqual(s.cpu.bus.read(0x04a0000c), 0x76543210)
            self.assertEqual(s.cpu.bus.read(0x04c00000), 0xc8000000)
            self.assertEqual(s.cpu.bus.read(0x04c00004), 0x00100c01)
            self.assertEqual(s.cpu.bus.read(0x040036c0), 0xc0010257)
            self.assertEqual(s.cpu.bus.read(0x04f10004, 1), 0)
            with self.assertRaises(EmulationStop):
                s.cpu.bus.read(0x04f10004, 4)
            s.cpu.bus.write(0x040032f4, 0)
            with self.assertRaises(EmulationStop) as opaque:
                s.cpu.bus.read(0x040032f4)
            self.assertEqual(opaque.exception.kind, 'unmodeled-device-readback')
            s.cpu.bus.write(0x04e3ff44, 7)
            self.assertEqual(s.cpu.bus.read(0x04e3ff44), 7)
            with self.assertRaises(EmulationStop):
                s.cpu.bus.read(0x04e3fbfc)  # adjacent unsampled bank stays unknown
            with self.assertRaises(EmulationStop):
                s.cpu.bus.read(0x0030affc)  # unmodeled GCTL offsets stay explicit
            self.assertEqual(s.cpu.bus.read(0x04600120), 0)
            self.assertEqual(s.cpu.bus.read(0x204004), 0)  # sparse I2C mapping preserves CIR
            self.assertEqual(s.snapshot()["profile"]["spi_jedec"], "c84016")
            self.assertFalse(MachineProfile.load("strict").timers)
            self.assertFalse(MachineProfile.load("exploratory").timers)

    def test_raw_and_usb_payload_detection_and_explicit_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.bin"
            raw = bytes.fromhex("aa55aa55") + bytes(32)
            path.write_bytes(raw)
            self.assertEqual(Firmware(path).offset, 0)
            path.write_bytes(bytes(256) + raw)
            self.assertEqual(Firmware(path).offset, 256)
            self.assertEqual(Firmware(path, offset=256).payload, raw)
            with self.assertRaises(ValueError):
                Firmware(path, offset=0)

    def test_invalid_timer_clock_and_flash_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            for fields in [{"timers": 1}, {"timer_input_hz": 0}, {"timer_input_hz": True},
                           {"spi_jedec": "ef40"}, {"spi_jedec": "xx4016"}]:
                path.write_text(json.dumps({"name": "test", "description": "invalid fixture", **fields}))
                with self.assertRaises(ValueError):
                    MachineProfile.load(path)


if __name__ == "__main__":
    unittest.main()
