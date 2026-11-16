"""EHCI init handshakes with observed capabilities and no USB device."""
import json
from pathlib import Path
import tempfile
import unittest

from starsat_emu.ehci import EHCI
from starsat_emu.firmware import Firmware
from starsat_emu.memory import EmulationStop
from starsat_emu.profile import MachineProfile


class EHCITests(unittest.TestCase):
    def setUp(self):
        self.tick = 0
        self.controller = EHCI(lambda: self.tick)

    def read(self, offset, size=4):
        return self.controller.access(offset, size)

    def write(self, offset, value, size=4):
        return self.controller.access(offset, size, value)

    def test_observed_capabilities_have_consistent_byte_halfword_and_word_views(self):
        for offset, value in [(0, 0x01000010), (4, 0x2112), (8, 0xa022), (0x10, 0x80000), (0x14, 0x1000)]:
            self.assertEqual(self.read(offset), value)
            self.assertEqual([self.read(offset + i, 1) for i in range(4)], list(value.to_bytes(4, 'little')))
            self.assertEqual(self.read(offset, 2) | self.read(offset + 2, 2) << 16, value)
        with self.assertRaises(EmulationStop):
            self.write(0, 0)

    def test_reset_self_clears_halts_and_resets_operational_state(self):
        self.write(0x18, 0x3f)
        self.write(0x24, 0x12345000)
        self.write(0x28, 0x12345660)
        self.write(0x50, 1)
        self.write(0x10, 0x80031)
        self.assertEqual(self.read(0x14) & 0xd000, 0xc000)
        self.write(0x10, 0x80002)
        self.assertEqual(self.read(0x10), 0x80000)
        self.assertEqual(self.read(0x14), 0x1000)
        for offset in (0x18, 0x1c, 0x20, 0x24, 0x28, 0x50):
            self.assertEqual(self.read(offset), 0)
        self.assertEqual(self.read(0x54), 0x2000)
        self.assertEqual(self.controller.snapshot()['reset_count'], 1)

    def test_run_halt_and_frame_counter_have_fractional_time_and_w1c_rollover(self):
        self.write(0x10, 0x80001)
        self.tick = 12
        self.assertEqual(self.read(0x1c), 0)
        self.tick = 13
        self.assertEqual(self.read(0x1c), 1)
        self.write(0x1c, 0x3fff)
        self.tick = 26
        self.assertEqual(self.read(0x1c), 0)
        self.assertEqual(self.read(0x14) & 8, 8)
        self.write(0x18, 8)
        self.assertTrue(self.controller.snapshot()['irq_pending'])
        self.write(0x15, 0xff, 1)  # upper byte cannot clear lower-byte W1C flags
        self.assertEqual(self.read(0x14) & 8, 8)
        self.write(0x14, 8, 1)
        self.assertFalse(self.controller.snapshot()['irq_pending'])
        self.write(0x10, 0x80000)
        value = self.read(0x1c)
        self.tick = 10000
        self.assertEqual(self.read(0x1c), value)
        self.assertEqual(self.read(0x14) & 0x1000, 0x1000)

    def test_empty_ports_never_invent_attach_enable_or_transfer_completion(self):
        self.write(0x50, 1)
        for offset in (0x54, 0x58):
            self.assertEqual(self.read(offset), 0)
            self.write(offset, 0x1000 | 0x100 | 0xf)
            self.assertEqual(self.read(offset), 0x1100)
            self.write(offset, 0x1000)
            self.assertEqual(self.read(offset), 0x1000)
        self.write(0x10, 0x80031)
        self.assertEqual(self.read(0x14) & 0x3f, 0)
        self.assertEqual(self.controller.snapshot()['attached_devices'], [])
        self.assertFalse(self.controller.snapshot()['dma_transfers_implemented'])
        self.write(0x10, 0x80071)  # async advance doorbell on empty schedule
        self.assertEqual(self.read(0x10) & 0x40, 0)
        self.assertEqual(self.read(0x14) & 0x3f, 0x20)
        self.write(0x14, 0x20)
        self.assertEqual(self.read(0x14) & 0x3f, 0)

    def test_partial_command_write_preserves_other_fields_and_rejects_unsupported_modes(self):
        self.write(0x10, 1, 1)
        self.assertEqual(self.read(0x10), 0x80001)
        for offset, value, size in [(0x10, 0x80080, 4), (0x10, 0x8000c, 4),
                                    (0x20, 1, 4), (0x54, 0x10000, 4),
                                    (0x60, 0, 4), (0x11, 0, 2)]:
            with self.assertRaises(EmulationStop):
                self.write(offset, value, size)

    def test_profile_opt_in_and_overlap_guard(self):
        self.assertFalse(MachineProfile.load('strict').ehci)
        self.assertFalse(MachineProfile.load('exploratory').ehci)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'raw.bin'
            image.write_bytes(bytes.fromhex('aa55aa55') + bytes(0x3000))
            path = Path(directory) / 'profile.json'
            path.write_text(json.dumps({'name': 'test', 'description': 'EHCI fixture', 'ehci': True}))
            cpu = Firmware(image).machine(profile=MachineProfile.load(path))
            self.assertEqual(cpu.bus.read(0x900000, 1), 0x10)
            cpu.bus.write(0x900010, 0x80002)
            self.assertEqual(cpu.bus.read(0x900014), 0x1000)
            for fields in [{'ehci': 1}, {'ehci': True, 'provisional_registers': {
                '0x900010': {'name': 'shadow', 'reset': 0}}}]:
                path.write_text(json.dumps({'name': 'test', 'description': 'invalid fixture', **fields}))
                with self.assertRaises(ValueError):
                    Firmware(image).machine(profile=MachineProfile.load(path))


if __name__ == '__main__':
    unittest.main()
