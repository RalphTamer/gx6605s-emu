"""Fixed AV writes, explicit timeouts, CRC transport and read-only handoff."""
from collections import deque
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import serial_av_startup_probe as av
from starsat_emu.accelerator import Accelerator


class AVStartupProbeTests(unittest.TestCase):
    def execute(self, stalled=False, first_image=0, running_status=False):
        code, waiting = av.program(sample_size=64, poll_budget=2, first_image=first_image, capture_running_status=running_status)
        bus = av.ProbeBus(stalled, first_image); bus.outgoing.extend(b'O')
        bus.map('dram', 0x10000000, 64 * 1024 * 1024)
        offset = (av.ram.ENTRY & 0x1fffffff) - 0x10000000
        bus.regions[0].data[offset:offset+len(code)] = code
        cpu = av.ram.CPU(bus, av.ram.ENTRY); cpu.cr[18] = 0x7d
        accelerator = Accelerator(cpu); history = deque(maxlen=32)
        while cpu.pc != waiting and cpu.steps < 5000000:
            accelerator.advance(cpu, 100000, history, (waiting,))
            if cpu.pc != waiting:cpu.step()
        self.assertEqual(cpu.pc, waiting); self.assertFalse(cpu.cr[0] & 0x40)
        self.assertEqual(bus.starts, 2); self.assertEqual(bus.registers[0], 0)
        return cpu, bytes(bus.outgoing)

    def test_exact_native_transcripts_capture_ram_and_hand_off_to_bounded_reader(self):
        cpu, frame = self.execute(); parsed = av.parse_reply(frame, sample_size=64)
        self.assertEqual([run['status'] for run in parsed['runs']], ['mailbox-zero'] * 2)
        for index in range(2):
            offset = 1 + 48 + index * 96 + 32 + 4 * index
            self.assertEqual(int.from_bytes(frame[offset:offset+4], 'little'), 0xaabbcc00 | index)
        bus = cpu.bus; before = len(bus.outgoing)
        bus.incoming.extend(av.monitor.request(av.BASE, 0x12345678, windows=av.WINDOWS))
        deadline = cpu.steps + 10000
        while len(bus.outgoing) < before + 20 and cpu.steps < deadline:cpu.step()
        reply = bytes(bus.outgoing[before:])
        self.assertEqual(av.monitor.parse_response(reply, av.BASE, 0x12345678), 0xe470e190)
        with self.assertRaises(ValueError):av.monitor.request(av.BASE + av.SIZE, 1, windows=av.WINDOWS)
        for length in range(len(frame)):
            self.assertIsNone(av.parse_reply(frame[:length], sample_size=64))
        for offset in (1, 21, 36, 55, 110, len(frame) - 17, len(frame) - 1):
            changed = bytearray(frame); changed[offset] ^= 1
            with self.assertRaises(ValueError):av.parse_reply(changed, sample_size=64)

    def test_timeout_is_captured_without_a_completed_boot_result(self):
        _, frame = self.execute(True)
        parsed = av.parse_reply(frame, sample_size=64)
        self.assertEqual([run['status'] for run in parsed['runs']], ['poll-timeout'] * 2)
        self.assertEqual([run['polls'] for run in parsed['runs']], [2, 2])
        self.assertEqual([run['mailbox_after'] for run in parsed['runs']], ['0x1', '0x1'])

    def test_v393_first_and_live_status_are_independent_crc_checked_fields(self):
        _, frame = self.execute(first_image=1, running_status=True)
        parsed = av.parse_reply(frame, sample_size=64)
        self.assertEqual(parsed['version'], 2)
        self.assertEqual([r['image'] for r in parsed['runs']], ['v393', 'receiver'])
        self.assertEqual([r['pre_stop_status_18'] for r in parsed['runs']], ['0x8b'] * 2)
        for offset in (37, 41, 45):
            changed = bytearray(frame); changed[offset] ^= 1
            with self.assertRaises(ValueError):av.parse_reply(changed, sample_size=64)
        with self.assertRaises(ValueError):av.program(first_image=1)

    def test_original_physical_capture_remains_valid_and_skipped_image_is_distinct(self):
        import hashlib
        root = Path(__file__).resolve().parents[1]
        frame = (root / 'traces/serial-av-startup-20261114T082509334521Z.bin').read_bytes()
        parsed = av.parse_reply(frame)
        self.assertEqual(parsed['runs'][0]['status'], 'mailbox-zero')
        self.assertEqual(parsed['runs'][1]['status'], 'unexpected-boot-status')
        self.assertEqual(parsed['runs'][1]['polls'], 0)
        self.assertEqual(hashlib.sha256(av.ram.wrap_stage2(av.program()[0])).hexdigest(),
                         'aabc080a63da4a49fe0711a2dcc0a036209f14fe2c8f70e7c918dcb643843a13')


if __name__ == '__main__':unittest.main()
