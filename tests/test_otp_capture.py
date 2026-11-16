"""Execute the fixed reader and reject partial/altered captures and fuse writes."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import serial_otp_read as reader
from starsat_emu.memory import EmulationStop
from starsat_emu.otp import OTP
from starsat_emu.profile import MachineProfile


class OTPCaptureTests(unittest.TestCase):
    def execute(self, stalled=False):
        content = bytes((i * 73 + 9) & 255 for i in range(32))
        code, idle = reader.program(len(content), poll_budget=2)
        bus = reader.ReaderBus(content, stalled)
        bus.outgoing.extend(b'O')
        bus.map('code', reader.ram.ENTRY & 0x1fffffff, len(code), code)
        cpu = reader.ram.CPU(bus, reader.ram.ENTRY)
        cpu.cr[18] = 0x7d
        cpu.cr[0] |= 0x40
        initial = cpu.cr[0]
        while cpu.pc != idle and cpu.steps < 100000:
            cpu.step()
        self.assertEqual(cpu.pc, idle)
        self.assertFalse(cpu.cr[0] & 0x40)
        self.assertEqual(struct.unpack_from('<I', bus.outgoing, 41)[0], initial)
        self.assertEqual(bus.otp.control, 0)
        return bus, content

    def test_exact_read_addresses_and_bytes_without_programming(self):
        bus, content = self.execute()
        result = reader.parse_reply(bytes(bus.outgoing), len(content))
        self.assertEqual(result['size'], len(content))
        self.assertEqual(bus.addresses, list(range(len(content))))
        self.assertEqual(bus.outgoing[1 + reader.HEADER_SIZE:-12], content)
        self.assertTrue(all(a == 0x403000 or (a == 0xf80000 and not v & ~0x7ff8)
                            for a, v, size in bus.device_writes))
        frame = bytes(bus.outgoing)
        for length in range(len(frame)):
            self.assertIsNone(reader.parse_reply(frame[:length], len(content)))
        for offset in (1, 21, 40, 64, len(frame) - 1):
            damaged = bytearray(frame); damaged[offset] ^= 1
            with self.assertRaises(ValueError):
                reader.parse_reply(damaged, len(content))

    def test_timeout_returns_explicit_failure_and_cleans_control(self):
        bus, content = self.execute(True)
        self.assertEqual(bus.addresses, [])
        with self.assertRaisesRegex(ValueError, 'status=1, captured=0'):
            reader.parse_reply(bytes(bus.outgoing), len(content))

    def test_reader_and_emulator_reject_programming_and_data_fields(self):
        bus = reader.ReaderBus(bytes(2048))
        otp = OTP()
        for value in (0x8000, 0xaa8f40, 0x10000, 1, 4):
            with self.assertRaises(ValueError):bus.access(0xf80000, 4, value)
            with self.assertRaises(EmulationStop):otp.access(0, 4, value)
        for value in (2, 0x8000, 0xaa8f40):
            with self.assertRaises(EmulationStop):otp.access(4, 4, value)
        for size in (0, 1, 2049, 4096, True):
            with self.assertRaises(ValueError):reader.program(size)

    def test_byte_zero_falling_read_edge_keeps_result_until_cleanup(self):
        otp = OTP(); otp.data = bytes([0xa7]) + bytes(2047)
        otp.access(0, 4, 0x4000)
        otp.access(0, 4, 0)
        self.assertEqual(otp.access(8, 4), 0x6a7)
        otp.access(0, 4, 0)
        self.assertEqual(otp.access(8, 4), 0x4a7)

    def test_profile_requires_a_complete_explicit_capture_and_reports_only_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            data = bytes(range(256)) * 8
            base = {'name': 'fixture', 'description': 'capture fixture', 'otp': True}
            path.write_text(json.dumps({**base, 'otp_data': data.hex()}))
            profile = MachineProfile.load(path)
            self.assertEqual(profile.summary()['otp_data']['size'], 2048)
            self.assertNotIn(data.hex(), json.dumps(profile.summary()))
            otp = OTP(data=bytes.fromhex(profile.otp_data))
            otp.access(0, 4, 0x4000 | (0x1e8 << 3))
            self.assertEqual(otp.access(8, 4) & 255, data[0x1e8])
            self.assertTrue(otp.captured)
            for raw, enabled in [('00', True), ('zz' * 2048, True), (data.hex(), False)]:
                path.write_text(json.dumps({**base, 'otp': enabled, 'otp_data': raw}))
                with self.assertRaises(ValueError):MachineProfile.load(path)


if __name__ == '__main__':unittest.main()
