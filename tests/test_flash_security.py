"""Read-only security-bank framing, isolation, missing data and rollover."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import serial_flash_dump as dump
from starsat_emu.memory import EmulationStop
from starsat_emu.profile import MachineProfile
from starsat_emu.spi import SerialNOR


def execute(stalled=False):
    content = bytes((i * 17 + 91) & 255 for i in range(1024))
    code, idle = dump.reader(1024, 2 if stalled else 1 << 20, dump.SECURITY_BANK)
    bus = dump.ReaderBus(b'unchanged main flash', stalled=stalled, security=content)
    bus.outgoing.extend(b'O')
    bus.map('code', dump.ram.ENTRY & 0x1fffffff, len(code), code)
    bus.map('capture', 0x10000000, 1024)
    cpu = dump.CPU(bus, dump.ram.ENTRY);cpu.cr[18] = 0x7d
    while cpu.pc != idle and cpu.steps < 150000:
        cpu.step()
    if cpu.pc != idle:raise AssertionError('security reader did not finish')
    return bus, content


class SecurityTests(unittest.TestCase):
    def test_all_banks_keep_separate_addresses_and_main_array_unchanged(self):
        banks = {bank: bytes([(i + 1) * 31]) * 1024 for i, bank in enumerate(dump.SECURITY_BANKS)}
        code, idle = dump.reader(3072, security_all=True)
        bus = dump.ReaderBus(b'unchanged main flash', security=banks)
        bus.outgoing.extend(b'O')
        bus.map('code', dump.ram.ENTRY & 0x1fffffff, len(code), code)
        bus.map('capture', 0x10000000, 3072)
        cpu = dump.CPU(bus, dump.ram.ENTRY); cpu.cr[18] = 0x7d
        while cpu.pc != idle and cpu.steps < 300000:cpu.step()
        self.assertEqual(cpu.pc, idle)
        result = dump.parse_frame(bytes(bus.outgoing), 3072, security_all=True)
        self.assertEqual(set(result['security_banks']), {'0x1000', '0x2000', '0x3000'})
        self.assertEqual(bus.outgoing[1 + dump.HEADER_SIZE:-len(dump.FOOTER)], b''.join(banks.values()))
        self.assertEqual(bus.commands, [0x9f, 0x48, 0x48, 0x48])
        self.assertFalse(bus.spi.flash.selected)
        self.assertEqual(bus.spi.flash.data[:20], b'unchanged main flash')
        self.assertEqual(bus.spi.flash.read_bytes, 0)
        damaged = bytearray(bus.outgoing); damaged[1100] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC32'):
            dump.parse_frame(bytes(damaged), 3072, security_all=True)

    def test_unique_id_requires_capture_and_four_bytes_before_data(self):
        flash=SerialNOR(b'main');flash.select(True)
        for byte in bytes.fromhex('4b00000000'):flash.exchange(byte)
        with self.assertRaisesRegex(EmulationStop,'factory ID'):flash.exchange(0)
        flash.unique_id=bytes(range(16))
        self.assertEqual(bytes(flash.exchange(0) for _ in range(16)),bytes(range(16)))
        with self.assertRaises(EmulationStop):flash.exchange(0)
        self.assertEqual(flash.read_bytes,0)
        flash.select(False);flash.select(True)
        for byte in bytes.fromhex('4b00000000'):flash.exchange(byte)
        self.assertEqual(flash.exchange(0),0)
    def test_actual_reader_captures_only_bank_three_and_validates_crc(self):
        bus, content = execute()
        result = dump.parse_frame(bus.outgoing, 1024, 0x3000)
        self.assertEqual(result['size'], 1024)
        self.assertEqual(bus.outgoing[1 + dump.HEADER_SIZE:-len(dump.FOOTER)], content)
        self.assertEqual(bus.commands, [0x9f, 0x48])
        self.assertEqual(bus.spi.flash.data[:20], b'unchanged main flash')
        self.assertEqual(bus.spi.flash.read_bytes, 0)
        self.assertFalse(bus.spi.flash.selected)
        frame = bytes(bus.outgoing)
        self.assertIsNone(dump.parse_frame(frame[:-1], 1024, 0x3000))
        damaged = bytearray(frame);damaged[100] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC32'):dump.parse_frame(damaged, 1024, 0x3000)
        with self.assertRaises(ValueError):dump.parse_frame(frame, 1024)
        with self.assertRaises(ValueError):dump.reader(1024, security_bank=0x1001)

    def test_timeout_deselects_chip_and_does_not_report_capture(self):
        bus, _ = execute(stalled=True)
        self.assertFalse(bus.spi.flash.selected)
        with self.assertRaisesRegex(ValueError, 'status=1'):
            dump.parse_frame(bus.outgoing, 1024, 0x3000)

    def test_missing_security_stops_and_captured_bank_wraps_without_touching_main_array(self):
        flash = SerialNOR(b'main flash')
        flash.select(True)
        for byte in bytes.fromhex('480033ff00'):flash.exchange(byte)
        with self.assertRaisesRegex(EmulationStop, 'outside the 4 MiB'):
            flash.exchange(0)
        flash.security[0x3000] = bytes(range(256)) * 4
        self.assertEqual(flash.exchange(0), 255)
        self.assertEqual(flash.exchange(0), 0)
        self.assertEqual(flash.data[:10], b'main flash')
        for command in (0x06, 0x02, 0x20, 0x42, 0x44):
            flash.select(False);flash.select(True)
            with self.assertRaises(EmulationStop):flash.exchange(command)

    def test_profile_accepts_only_explicit_complete_security_banks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            base = {'name': 'fixture', 'description': 'synthetic fixture', 'spi_flash': True}
            for mapping in ({'0x3000': '00' * 1024}, {'0x3000': 'ff' * 1024}):
                path.write_text(json.dumps({**base, 'spi_security': mapping}))
                p = MachineProfile.load(path)
                self.assertEqual(len(p.spi_security[0x3000]), 1024)
                self.assertEqual(p.summary()['spi_security']['0x3000']['size'], 1024)
            for mapping in ({'0x3000': '00'}, {'0x3001': '00' * 1024}, {'0x3000': 'zz' * 1024}):
                path.write_text(json.dumps({**base, 'spi_security': mapping}))
                with self.assertRaises(ValueError):MachineProfile.load(path)


if __name__ == '__main__':unittest.main()
