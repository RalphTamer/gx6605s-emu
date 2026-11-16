"""RTC encodings required by both receiver kernels' time-conversion code."""
import unittest
from starsat_emu.memory import EmulationStop
from starsat_emu.rtc import RTC


class RTCBCDTests(unittest.TestCase):
    def setUp(self):
        self.ticks = 0
        self.rtc = RTC(lambda: self.ticks)
        self.rtc.access(0, 4, 1)

    def test_milliseconds_remain_binary_but_calendar_time_is_bcd(self):
        self.ticks = (12 * 3600 + 34 * 60 + 56) * 100000 + 78900
        self.assertEqual(self.rtc.access(0x68, 4), 789)
        self.assertEqual(self.rtc.access(0x6c, 4), 0x56)
        self.assertEqual(self.rtc.access(0x70, 4), 0x34)
        self.assertEqual(self.rtc.access(0x74, 4), 0x12)

    def test_rollover_and_bcd_writes_preserve_other_counter_fields(self):
        self.ticks = 5999999
        self.assertEqual(self.rtc.access(0x6c, 4), 0x59)
        self.ticks += 1
        self.assertEqual(self.rtc.access(0x6c, 4), 0)
        self.assertEqual(self.rtc.access(0x70, 4), 1)
        self.rtc.access(0x6c, 4, 0x42)
        self.assertEqual(self.rtc.access(0x6c, 4), 0x42)
        self.assertEqual(self.rtc.access(0x70, 4), 1)
        self.rtc.access(0, 4, 0)
        self.ticks += 100000
        self.assertEqual(self.rtc.access(0x6c, 4), 0x42)

    def test_invalid_bcd_and_out_of_range_time_do_not_update_counter(self):
        for offset, value in ((0x6c, 0x1a), (0x6c, 0x60), (0x74, 0x24)):
            with self.assertRaises(EmulationStop):
                self.rtc.access(offset, 4, value)
        self.assertEqual(self.rtc.access(0x6c, 4), 0)


if __name__ == '__main__':
    unittest.main()
