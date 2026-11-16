"""Explicit register widths and unknown reset values for device bring-up."""
import json
from pathlib import Path
import tempfile
import unittest

from starsat_emu.firmware import Firmware
from starsat_emu.memory import EmulationStop
from starsat_emu.profile import MachineProfile


class ProvisionalRegisters(unittest.TestCase):
    def profile(self, directory, registers):
        path = Path(directory) / 'profile.json'
        path.write_text(json.dumps({'name': 'test', 'description': 'explicit register fixture',
                                    'provisional_registers': registers}))
        return MachineProfile.load(path)

    def machine(self, directory, registers):
        image = Path(directory) / 'firmware.bin'
        image.write_bytes(bytes.fromhex('aa55aa55') + bytes(0x3000))
        return Firmware(image).machine(profile=self.profile(directory, registers))

    def test_adjacent_bytes_and_halfwords_do_not_alias_or_widen_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            cpu = self.machine(directory, {
                '0x04f10004': {'name': 'byte 1', 'size': 1, 'reset': 0xa5},
                '0x04f10005': {'name': 'byte 2', 'size': 1, 'reset': 0x5a},
                '0x04f10006': {'name': 'halfword', 'size': 2, 'reset': 0x5678}})
            bus = cpu.bus
            self.assertEqual(bus.read(0x04f10004, 1), 0xa5)
            bus.write(0x04f10004, 0x142, 1)
            self.assertEqual(bus.read(0x04f10004, 1), 0x42)
            self.assertEqual(bus.read(0x04f10005, 1), 0x5a)
            self.assertEqual(bus.read(0x04f10006, 2), 0x5678)
            for address, size in [(0x04f10004, 4), (0x04f10004, 2), (0x04f10006, 1), (0x04f10008, 1)]:
                with self.assertRaises(EmulationStop):
                    bus.read(address, size)

    def test_null_reset_stops_until_guest_writes_and_never_supplies_a_default(self):
        with tempfile.TemporaryDirectory() as directory:
            cpu = self.machine(directory, {'0x04c00008': {'name': 'write-initialized config', 'reset': None}})
            with self.assertRaises(EmulationStop) as caught:
                cpu.bus.read(0x04c00008)
            self.assertEqual(caught.exception.kind, 'unknown-device-reset')
            cpu.bus.write(0x04c00008, 0x12345678)
            self.assertEqual(cpu.bus.read(0x04c00008), 0x12345678)
            # A fresh boot does not inherit the previous machine's write.
            fresh = self.machine(directory, {'0x04c00008': {'name': 'write-initialized config', 'reset': None}})
            with self.assertRaises(EmulationStop):
                fresh.bus.read(0x04c00008)

    def test_invalid_width_values_alignment_and_overlapping_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for registers in [
                {'0x04f10004': {'name': 'bad', 'size': 3, 'reset': 0}},
                {'0x04f10004': {'name': 'bad', 'size': True, 'reset': 0}},
                {'0x04f10005': {'name': 'bad', 'size': 2, 'reset': 0}},
                {'0x04f10004': {'name': 'bad', 'size': 1, 'reset': 256}},
                {'0x04f10004': {'name': 'bad', 'reset': True}},
                {'0x04f10004': {'name': 'bad', 'reset': 0, 'readback': False}},
                {'0x04f10004': {'name': 'bad', 'reset': None, 'readback': 1}},
                {'0x04f10004': {'name': 'bad', 'reset': None, 'write_clear': True}},
                {'0x04f10004': {'name': 'bad', 'reset': 0, 'write_clear': 1}},
                {'0x04f10004': {'name': 'word', 'reset': 0},
                 '0x04f10005': {'name': 'overlap', 'size': 1, 'reset': 0}},
                {'0x04f10004': {'name': 'word', 'reset': 0},
                 '0x4f10004': {'name': 'same address', 'reset': 0}},
            ]:
                with self.assertRaises(ValueError):
                    self.profile(directory, registers)

    def test_opaque_writes_never_become_synthetic_status_or_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            cpu = self.machine(directory, {'0x0400350c': {
                'name': 'opaque initialization write', 'reset': None, 'readback': False}})
            for value in (None, 0xffffffff, 0, 1):
                if value is not None:
                    cpu.bus.write(0x0400350c, value)
                    self.assertEqual(cpu.bus.provisional[0x0400350c]['value'], value)
                with self.assertRaises(EmulationStop) as caught:
                    cpu.bus.read(0x0400350c)
                self.assertEqual(caught.exception.kind, 'unmodeled-device-readback')

    def test_write_clear_masks_existing_events_and_never_generates_ready_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            cpu = self.machine(directory, {'0x04600118': {
                'name': 'write-clear event fixture', 'reset': 0, 'write_clear': True}})
            cpu.bus.write(0x04600118, 0x40)
            cpu.bus.write(0x04600118, 0x80)
            self.assertEqual(cpu.bus.read(0x04600118), 0)
            cpu.bus.provisional[0x04600118]['value'] = 0xc0  # independent event injection fixture
            cpu.bus.write(0x04600118, 0x40)
            self.assertEqual(cpu.bus.read(0x04600118), 0x80)
            cpu.bus.write(0x04600118, 0)
            self.assertEqual(cpu.bus.read(0x04600118), 0x80)
            cpu.bus.write(0x04600118, 0x80)
            self.assertEqual(cpu.bus.read(0x04600118), 0)

    def test_provisional_entry_cannot_shadow_modeled_ram_or_uart(self):
        with tempfile.TemporaryDirectory() as directory:
            for address in ('0x100000', '0x400008'):
                with self.assertRaisesRegex(ValueError, 'overlaps modeled'):
                    self.machine(directory, {address: {'name': 'shadow', 'size': 1, 'reset': 0}})


if __name__ == '__main__':
    unittest.main()
