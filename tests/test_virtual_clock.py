"""All clocked peripherals use the same configurable instruction rate."""
import json
from pathlib import Path
import tempfile
import unittest
from starsat_emu.ehci import EHCI
from starsat_emu.ohci import OHCI
from starsat_emu.profile import MachineProfile
from starsat_emu.rtc import RTC
from starsat_emu.timer import Timers


class VirtualClockTests(unittest.TestCase):
    def test_ten_million_instruction_clock_has_consistent_rtc_usb_and_timer_units(self):
        ticks = [0]
        clock = lambda: ticks[0]
        rtc = RTC(clock, 10000000)
        timer = Timers(clock, 27000000, 10000000)
        ehci = EHCI(clock, 10000000)
        ohci = OHCI(0xa00000, clock, 10000000)
        rtc.access(0, 4, 1)
        timer.access(0x24, 4, 26)  # 1 MHz timer count input
        timer.access(0x20, 4, 1)
        timer.access(0x10, 4, 2)
        ehci.access(0x10, 4, 0x80001)
        ohci.access(4, 4, 0x80)
        ticks[0] = 10000  # exactly 1 ms
        self.assertEqual(rtc.access(0x68, 4), 1)
        self.assertEqual(timer.access(4, 4), 1000)
        self.assertEqual(ehci.access(0x1c, 4), 8)
        self.assertEqual(ohci.access(0x3c, 4), 1)
        ticks[0] = 10000000
        self.assertEqual(rtc.access(0x6c, 4), 1)
        self.assertEqual(timer.access(4, 4), 1000000)
        self.assertEqual(ehci.access(0x1c, 4), 8000)
        self.assertEqual(ohci.access(0x3c, 4), 1000)

    def test_timer_deadline_scales_with_instruction_rate(self):
        ticks = [0]
        timer = Timers(lambda: ticks[0], 27000000, 10000000)
        for offset, value in ((0x24, 26), (0x28, 0xfffffff6), (0x20, 3), (0x10, 2)):
            timer.access(offset, 4, value)
        self.assertEqual(timer.instructions_to_irq(), 100)
        ticks[0] = 99
        self.assertFalse(timer.irq_pending())
        self.assertEqual(timer.instructions_to_irq(), 1)
        ticks[0] = 100
        self.assertTrue(timer.irq_pending())
        self.assertEqual(timer.access(4, 4), 0xfffffff6)

    def test_invalid_rates_and_incomplete_interrupt_configuration_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            for fields in ({'instruction_hz': 0}, {'instruction_hz': True}, {'instruction_hz': 1000000001},
                           {'timer_interrupts': True}, {'timer_interrupts': True, 'timers': True}):
                path.write_text(json.dumps({'name': 'test', 'description': 'invalid fixture', **fields}))
                with self.assertRaises(ValueError):
                    MachineProfile.load(path)


if __name__ == '__main__':
    unittest.main()
