"""Controller sequences must complete without inventing I2C slave ACKs."""
import unittest
from starsat_emu.i2c import I2C, I2CControllers
from starsat_emu.memory import EmulationStop


class I2CTests(unittest.TestCase):
    def test_controller_registers_are_independent_and_holes_do_not_hide_cir(self):
        c = I2CControllers()
        c.access(8, 4, 0x420042)
        c.access(0x2008, 4, 0x850085)
        c.access(0x3008, 4, 0x220022)
        self.assertEqual(c.access(8, 4), 0x420042)
        self.assertEqual(c.access(0x2008, 4), 0x850085)
        self.assertEqual(c.access(0x3008, 4), 0x220022)
        self.assertEqual(len(c.snapshot()["controllers"]), 3)
        self.assertFalse(c.contains(0x204000, 4))
        self.assertTrue(c.contains(0x205008, 4))
        with self.assertRaises(EmulationStop):
            c.access(0x100, 4)
    def test_xboot_address_nack_stop_and_disable_sequence(self):
        c = I2C()
        c.access(8, 4, 0x00420042)
        self.assertEqual(c.access(8, 4), 0x00420042)
        c.access(4, 4, 0xa0)
        c.access(0, 4, c.ENABLE | c.START)
        self.assertTrue(c.access(0, 4) & c.DONE)
        self.assertTrue(c.access(0, 4) & c.WACK)
        self.assertEqual(c.snapshot()["recent_transactions"][-1],
                         {"operation": "address", "address": 0x50, "read": False, "ack": False})
        c.access(0, 4, c.ENABLE | c.STOP)
        self.assertTrue(c.access(0, 4) & c.DONE)
        self.assertFalse(c.access(0, 4) & c.WACK)
        c.access(0, 4, 0)
        self.assertEqual(c.access(0, 4), 0)
        self.assertFalse(c.active)

    def test_read_direction_and_undriven_data_never_claim_a_slave_ack(self):
        c = I2C()
        c.access(4, 4, 0xa1)
        c.access(0, 4, c.ENABLE | c.START | c.INTEN)
        self.assertTrue(c.snapshot()["recent_transactions"][-1]["read"])
        self.assertTrue(c.snapshot()["irq_pending"])
        c.access(0, 4, c.ENABLE | c.RACK)
        self.assertEqual(c.access(4, 4), 255)
        self.assertTrue(c.access(0, 4) & c.WACK)
        self.assertEqual(c.snapshot()["slaves"], [])

    def test_status_from_guest_does_not_forge_ack_and_reserved_accesses_stop(self):
        c = I2C()
        c.access(0, 4, c.ENABLE | c.DONE)
        self.assertTrue(c.access(0, 4) & c.WACK)
        for offset, size, value in [(12, 4, None), (0, 1, None), (0, 4, 0x100), (0, 4, 1)]:
            with self.assertRaises(EmulationStop):
                c.access(offset, size, value)


if __name__ == "__main__":
    unittest.main()
