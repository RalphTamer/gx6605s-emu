"""Clocked raster scan position rather than a constant startup response."""
import json
from pathlib import Path
import tempfile
import unittest
from starsat_emu.memory import EmulationStop, VideoRegisters, VideoOutput, Bus
from starsat_emu.profile import MachineProfile


class RasterTests(unittest.TestCase):
    def test_scan_line_advances_crosses_safe_line_and_wraps_at_frame_boundary(self):
        ticks = [0]
        video = VideoRegisters(lambda: ticks[0], 10000000, {'frame_hz': 50, 'lines': 1125})
        self.assertEqual(video.access(0xe4, 4), 0)
        ticks[0] = 10000  # one millisecond, beyond line 49
        self.assertEqual(video.access(0xe4, 4), 56)
        ticks[0] = 100000  # half a frame
        self.assertEqual(video.access(0xe4, 4), 562)
        ticks[0] = 200000
        self.assertEqual(video.access(0xe4, 4), 0x800)
        ticks[0] = 400000
        self.assertEqual(video.access(0xe4, 4), 0)
        self.assertNotIn(0xe4, video.zero_reads)
        self.assertTrue(video.timing_snapshot()['enabled'])
        with self.assertRaises(EmulationStop):
            video.access(0xe4, 4, 50)

    def test_unconfigured_profile_retains_explicit_old_storage_behavior(self):
        video = VideoRegisters()
        self.assertEqual(video.access(0xe4, 4), 0)
        self.assertIn(0xe4, video.zero_reads)
        self.assertFalse(video.timing_snapshot()['enabled'])

    def test_interlaced_fields_use_observed_bit_11_and_half_line_frame_boundary(self):
        ticks = [0]
        video = VideoRegisters(lambda: ticks[0], 10000000, {'frame_hz': 25, 'lines': 625, 'interlaced': True})
        self.assertEqual(video.access(0xe4, 4), 0)
        ticks[0] = 100000  # 10 ms, first field
        self.assertEqual(video.access(0xe4, 4), 156)
        ticks[0] = 200000  # second field begins after 312.5 lines
        self.assertEqual(video.access(0xe4, 4), 0x800)
        ticks[0] = 300000
        self.assertEqual(video.access(0xe4, 4), 0x800 | 156)
        ticks[0] = 400000
        self.assertEqual(video.access(0xe4, 4), 0)

    def test_invalid_timing_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            for fields in [{'video_timing': {'frame_hz': 50, 'lines': 1125}},
                           {'video_registers': True, 'video_timing': {'frame_hz': True, 'lines': 1125}},
                           {'video_registers': True, 'video_timing': {'frame_hz': 50, 'lines': 2049}}]:
                path.write_text(json.dumps({'name': 'test', 'description': 'invalid fixture', **fields}))
                with self.assertRaises(ValueError):
                    MachineProfile.load(path)

    def test_guest_mode_changes_advance_progressive_and_interlaced_safe_windows(self):
        bus = Bus()
        bus.video = VideoRegisters(lambda: bus.ticks, 10000000, {'frame_hz': 25, 'lines': 625, 'interlaced': True})
        bus.video_output = VideoOutput(bus.video, 0x41f00000)  # measured bootstrap baseline
        bus.ticks = 200000
        self.assertEqual(bus.read(0x048000e4), 0x800)
        bus.write(0x04804000, 0x00060009)  # installed image selects 576p50
        bus.ticks += 180000
        self.assertGreater(bus.read(0x048000e4), 559)
        self.assertEqual(bus.read(0x04804000), 0x60009)
        bus.write(0x04804000, 0x00060026)  # V3.93 selects 1080i50
        bus.ticks += 190000
        self.assertGreater(bus.read(0x048000e4), 529)
        bus.ticks += 10000
        self.assertEqual(bus.read(0x048000e4), 0x800)
        # Changing non-timing bits preserves the position; unsupported formats
        # stop without changing the previously configured output.
        bus.write(0x04804000, 0x00020026)
        self.assertEqual(bus.read(0x048000e4), 0x800)
        with self.assertRaises(EmulationStop):
            bus.write(0x04804000, 31)
        self.assertEqual(bus.read(0x04804000), 0x20026)


if __name__ == '__main__':
    unittest.main()
