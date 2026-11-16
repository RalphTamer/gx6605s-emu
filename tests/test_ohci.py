"""OHCI reset/interrupt commands and empty-root-hub behavior."""
import unittest
from starsat_emu.memory import EmulationStop
from starsat_emu.ohci import OHCI, OHCIControllers


class OHCITests(unittest.TestCase):
    def setUp(self):
        self.tick = 0
        self.c = OHCI(0xa00000, lambda: self.tick)

    def read(self, offset):
        return self.c.access(offset, 4)

    def write(self, offset, value):
        self.c.access(offset, 4, value)

    def test_baseline_reset_completion_and_suspend_state(self):
        self.assertEqual(self.read(0), 0x110)
        self.assertEqual(self.read(0x34), 0x2edf)
        self.assertEqual(self.read(0x48), 0x02000901)
        self.write(0x18, 0x12345678)
        self.write(0x44, 123)
        self.write(4, 0x80)
        self.write(8, 1)
        self.assertEqual(self.read(8), 0)
        self.assertEqual(self.read(4) & 0xc0, 0xc0)
        self.assertEqual(self.read(0x18), 0)
        self.assertEqual(self.read(0x44), 0x628)
        self.assertEqual(self.c.snapshot()['reset_count'], 1)

    def test_interrupt_set_clear_aliases_master_mask_and_frame_rollover(self):
        self.write(0x10, 0x80000024)
        self.assertEqual(self.read(0x14), 0x80000024)
        self.write(4, 0x80)
        self.tick = 100
        self.assertEqual(self.read(0x3c), 1)
        self.assertEqual(self.read(0xc), 4)
        self.assertTrue(self.c.snapshot()['irq_pending'])
        self.write(0xc, 4)
        self.assertEqual(self.read(0xc), 0)
        self.write(0x14, 0x80000000)
        self.tick = 200
        self.assertFalse(self.c.snapshot()['irq_pending'])
        self.tick = 0x1000000
        self.assertEqual(self.read(0xc) & 0x20, 0x20)
        self.write(4, 0xc0)
        frame = self.read(0x3c)
        self.tick += 200
        self.assertEqual(self.read(0x3c), frame)

    def test_empty_port_power_commands_do_not_create_connect_or_reset_completion(self):
        self.write(0x54, 0x100)
        self.assertEqual(self.read(0x54), 0x100)
        self.write(0x54, 0x12)
        self.assertEqual(self.read(0x54), 0x100)
        self.write(0x54, 0x200)
        self.assertEqual(self.read(0x54), 0)
        self.write(0x50, 0x8000)
        self.assertEqual(self.read(0x50), 0x8000)
        self.write(0x50, 0x80000000)
        self.assertEqual(self.read(0x50), 0)
        self.assertEqual(self.c.snapshot()['attached_devices'], [])

    def test_transfer_requests_and_unknown_registers_stop_without_done_interrupt(self):
        for value in (2, 4, 6):
            with self.assertRaises(EmulationStop) as error:
                self.write(8, value)
            self.assertEqual(error.exception.kind, 'unimplemented-usb-transfer')
        self.assertEqual(self.read(0xc) & 2, 0)
        for offset in (0x58, 0x100):
            with self.assertRaises(EmulationStop):
                self.read(offset)
        with self.assertRaises(EmulationStop):
            self.write(0, 0)

    def test_sparse_controllers_have_independent_state_and_exclude_the_hole(self):
        controllers = OHCIControllers(lambda: self.tick)
        controllers.access(4, 4, 0x80)
        self.tick = 300
        self.assertEqual(controllers.access(0x3c, 4), 3)
        self.assertEqual(controllers.access(0x10003c, 4), 0)
        self.assertFalse(controllers.contains(0xa01000, 4))
        self.assertFalse(controllers.contains(0xaffffc, 4))
        self.assertTrue(controllers.contains(0xb00000, 4))


if __name__ == '__main__':
    unittest.main()
