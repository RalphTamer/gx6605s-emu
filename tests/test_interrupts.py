"""Timer level -> controller gate/mask -> ABIv1 IRQ -> RTE round trips."""
from collections import deque
import unittest
from starsat_emu.accelerator import Accelerator
from starsat_emu.cpu import CPU
from starsat_emu.memory import Bus, InterruptMasks
from starsat_emu.timer import Timers
from starsat_emu.graphics import GraphicsAccelerator


class TimerIRQTests(unittest.TestCase):
    def machine(self, target=0x100200):
        bus = Bus()
        bus.map('ram', 0x100000, 0x1000)
        bus.write(0x100000 + 40, target)
        bus.write(0x100100, 1, 2)
        bus.write(0x100102, 1, 2)
        bus.write(0x100104, 1, 2)
        bus.write(0x100200, 1, 2)
        bus.write(0x100202, 2, 2)  # rte
        bus.interrupt_masks = InterruptMasks(full=True)
        for i in range(16):
            bus.interrupt_masks.registers[0x60+i*4] = int.from_bytes(bytes(range(i*4,i*4+4)),'little')
        bus.timer_interrupts = True
        bus.timers = Timers(lambda: bus.ticks, input_hz=1000000)
        bus.interrupt_masks.registers[0x40] = 1 << 10
        c = CPU(bus, 0x100100)
        c.cr[0], c.cr[1] = 0x80000140, 0x100000
        bus.timers.access(0x20, 4, 3)
        bus.timers.access(0x28, 4, 0xffffff00)
        bus.timers.access(0x10, 4, 2)
        # Counter is near the end of a period; reload leaves enough time
        # to acknowledge and return before the next event.
        bus.timers.channels[0]['count'] = 0xfffffff0
        return c

    def assert_irq_after_overflow(self, c):
        c.bus.ticks = 2
        c.step()
        self.assertEqual(c.pc, 0x100202)
        self.assertEqual(c.cr[4], 0x100100)
        self.assertEqual(c.cr[2] & 0x7f0000, 10 << 16)
        self.assertEqual(c.cr[0] & 0x140, 0)
        self.assertEqual(c.interrupt_count, 1)

    def test_pending_gate_mask_and_cpu_interrupt_enable_are_independent(self):
        c = self.machine()
        c.bus.ticks = 2
        c.bus.interrupt_masks.registers[0x50] = 1 << 10
        self.assertFalse(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500000), 0)
        self.assertEqual(c.bus.read(0x500010), 1 << 10)
        c.bus.interrupt_masks.registers[0x50] = 0
        self.assertTrue(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500000), 1 << 10)
        c.bus.interrupt_masks.registers[0x40] = 0
        self.assertFalse(c.interrupt_ready())
        c.bus.interrupt_masks.registers[0x40] = 1 << 10
        for psr in (0x80000100, 0x80000040, 0x80000000):
            c.cr[0] = psr
            self.assertFalse(c.interrupt_ready())

    def test_irq_saves_state_and_rte_restores_pc_psr_and_carry_after_ack(self):
        c = self.machine()
        c.c = 1
        self.assert_irq_after_overflow(c)
        self.assertEqual(c.cr[2] & 1, 1)
        c.bus.write(0x20a000, 1)
        c.step()
        self.assertEqual(c.pc, 0x100100)
        self.assertEqual(c.control(0), 0x80000141)
        self.assertEqual(c.idly4, 0)
        self.assertFalse(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500000), 0)

    def test_odd_vector_selects_alternate_bank_and_return_restores_original(self):
        c = self.machine(0x100201)
        c.regs[3], c.alternate[3] = 17, 29
        self.assert_irq_after_overflow(c)
        self.assertEqual(c.regs[3], 29)
        self.assertEqual(c.cr[0] & 2, 2)
        c.bus.write(0x20a000, 1)
        c.step()
        self.assertEqual(c.regs[3], 17)
        self.assertEqual(c.cr[0] & 2, 0)

    def test_native_batch_stops_at_timer_deadline_before_next_instruction(self):
        c = self.machine()
        accelerator = Accelerator(c)
        if accelerator.native is None:
            self.skipTest('native interpreter unavailable')
        self.assertEqual(accelerator.advance(c, 100, deque(maxlen=32)), 2)
        self.assertEqual((c.pc, c.bus.ticks), (0x100104, 2))
        self.assertEqual(accelerator.advance(c, 100, deque(maxlen=32)), 0)
        c.step()
        self.assertEqual(c.pc, 0x100202)
        self.assertEqual(c.cr[4], 0x100104)

    def test_idly4_defers_pending_irq_for_four_instructions(self):
        c = self.machine()
        for pc in range(0x100100, 0x10010c, 2):
            c.bus.write(pc, 1, 2)
        c.idly4 = 4
        c.bus.ticks = 2
        for expected in range(3, -1, -1):
            c.step()
            self.assertEqual(c.interrupt_count, 0)
            self.assertEqual(c.idly4, expected)
        c.step()
        self.assertEqual(c.interrupt_count, 1)

    def test_graphics_irq_62_uses_high_status_bank_and_same_cpu_vector(self):
        c=self.machine();c.bus.timer_interrupts=False
        c.bus.graphics=GraphicsAccelerator(c.bus);c.bus.graphics.access(0x1c,4,0x101);c.bus.graphics.pending=True
        intc=c.bus.interrupt_masks
        intc.registers[0x44]=1<<30;intc.registers[0x54]=1<<30
        self.assertFalse(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500014),1<<30)
        self.assertEqual(c.bus.read(0x500004),0)
        intc.registers[0x54]=0
        self.assertTrue(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500004),1<<30)
        accelerator=Accelerator(c)
        self.assertEqual(accelerator.advance(c,100,deque(maxlen=32)),0)
        c.step();self.assertEqual(c.pc,0x100202)
        self.assertEqual(c.cr[4],0x100100)
        c.bus.graphics.access(0x1c,4,0x103)
        c.step();self.assertEqual(c.pc,0x100100)
        self.assertFalse(c.interrupt_ready())

    def test_timer_refresh_preserves_pending_graphics_irq(self):
        c=self.machine();c.bus.graphics=GraphicsAccelerator(c.bus)
        c.bus.graphics.access(0x1c,4,0x101)
        c.bus.graphics.pending=True;c.bus.interrupt_masks.registers[0x44]=1<<30
        c.bus.interrupt_masks.registers[0x50]=1<<10
        self.assertTrue(c.interrupt_ready())
        self.assertTrue(c.bus.interrupt_masks.sources & (1<<62))
        c.bus.graphics.access(0x1c,4,0x103)
        self.assertFalse(c.interrupt_ready())

    def test_source_selector_routes_graphics_to_a_different_output(self):
        c=self.machine();c.bus.timer_interrupts=False
        c.bus.graphics=GraphicsAccelerator(c.bus);c.bus.graphics.access(0x1c,4,0x101)
        c.bus.graphics.pending=True;intc=c.bus.interrupt_masks
        intc.access(0x40,4,1<<5)
        self.assertFalse(c.interrupt_ready())
        # Output 5's selector is byte 1 of SOURCE1.
        selector=intc.registers[0x64]
        intc.access(0x64,4,(selector & ~0xff00) | (62<<8))
        self.assertTrue(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500000),1<<5)
        intc.access(0x64,4,selector)
        self.assertFalse(c.interrupt_ready())

    def test_routed_timer_deadline_bounds_native_batch_in_high_bank(self):
        c=self.machine();intc=c.bus.interrupt_masks
        intc.access(0x40,4,0);intc.access(0x44,4,1<<8)
        intc.access(0x88,4,(intc.registers[0x88] & ~255) | 10)
        accelerator=Accelerator(c)
        if accelerator.native is None:self.skipTest('native interpreter unavailable')
        self.assertEqual(accelerator.advance(c,100,deque(maxlen=32)),2)
        self.assertTrue(c.interrupt_ready())
        self.assertEqual(c.bus.read(0x500004),1<<8)


if __name__ == '__main__':
    unittest.main()
