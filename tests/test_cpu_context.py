"""ABIv1 scheduler PSR restore and IDLY4 interpreter/native boundaries."""
from collections import deque
import unittest
from starsat_emu.accelerator import Accelerator
from starsat_emu.memory import EmulationStop
from test_emulator import machine


class ContextTests(unittest.TestCase):
    def test_full_psr_restore_updates_carry_coprocessor_and_interrupt_mask(self):
        c = machine(0x1801)  # mtcr r1, cr0
        c.cr[0], c.cp = 0x8f000100, 15
        c.regs[1] = 0x80000151
        c.step()
        self.assertEqual(c.control(0), 0x80000151)
        self.assertEqual(c.cp, 0)
        self.assertEqual(c.c, 1)

    def test_psr_af_roundtrip_swaps_all_sixteen_registers(self):
        c = machine(0x1801)
        c.regs[:] = list(range(16))
        c.alternate[:] = list(range(100, 116))
        c.regs[1] = 0x80000142
        previous = list(c.regs)
        c.step()
        self.assertEqual(c.regs, list(range(100, 116)))
        self.assertEqual(c.alternate, previous)
        c.control(0, 0x80000140)
        self.assertEqual(c.regs, previous)
        self.assertEqual(c.alternate, list(range(100, 116)))

    def test_idly4_clears_carry_and_counts_four_subsequent_instructions(self):
        c = machine(7, 1, 1, 1, 1, 0x6002)
        c.c = 1
        accelerator, history = Accelerator(c), deque(maxlen=32)
        self.assertEqual(accelerator.advance(c, 20, history), 0)
        c.step()
        self.assertEqual((c.c, c.idly4), (0, 4))
        for remaining in (3, 2, 1, 0):
            self.assertEqual(accelerator.advance(c, 20, history), 0)
            c.step()
            self.assertEqual(c.idly4, remaining)

    def test_trace_modes_and_privileged_user_instructions_remain_explicit_stops(self):
        c = machine(0x1801)
        c.regs[1] = 0x80004140
        with self.assertRaises(EmulationStop) as trace:
            c.step()
        self.assertEqual(trace.exception.kind, 'unsupported-psr-write')
        c.regs[1] = 0x80000140
        c.cr[0] = 0x140
        with self.assertRaises(EmulationStop) as privilege:
            c.step()
        self.assertEqual(privilege.exception.kind, 'privilege-exception')


if __name__ == '__main__':
    unittest.main()
