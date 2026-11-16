"""Differential finite I2C waits: preserve every guest timeout instruction."""
from collections import deque
import struct
import unittest
from starsat_emu.accelerator import Accelerator
from starsat_emu.cpu import CPU
from starsat_emu.i2c import I2CControllers
from starsat_emu.memory import Bus, InterruptMasks
from starsat_emu.timer import Timers


class I2CPollAccelerationTests(unittest.TestCase):
    def fixture(self, counter, control):
        bus = Bus(); bus.map('ram', 0x100000, 4096)
        bus.i2c = I2CControllers(); bus.i2c.controllers[1].control = control
        # Include both the counter timeout and successful exit paths.
        words = (0x8602, 0x8606, 0x2ea6, 0x2a86, 0xe805, 0x2407,
                 0x2a07, 0xe7f8, 0x60b2, 0xf001, 0x6002, 0x00cf)
        bus.regions[0].data[:len(words)*2] = struct.pack('<12H', *words)
        bus.write(0x100100, 0x205000)
        cpu = CPU(bus, 0x100002); cpu.regs[2] = 0x100100
        cpu.regs[6] = 0x205000; cpu.regs[7] = counter
        return cpu

    def test_native_ram_prefix_batches_the_mmio_boundary_before_fallback(self):
        cpu, ref = self.fixture(10000, 10), self.fixture(10000, 10)
        cpu.pc = ref.pc = 0x100000
        accelerator = Accelerator(cpu)
        if accelerator.native is None:self.skipTest('native interpreter unavailable')
        history, expected = deque(maxlen=32), deque(maxlen=32)
        done = accelerator.advance(cpu, 1000, history)
        self.assertGreater(done, 900)
        for _ in range(done):
            ref.step(); expected.append((*ref.last_instruction, None))
        self.assertEqual(cpu.snapshot(), ref.snapshot())
        self.assertEqual(history, expected)

    def test_matches_reference_time_history_device_events_and_counter(self):
        for counter in (2, 3, 11, 100000):
            for control in (0, 2, 10, 0x4a):
                for budget in (7, 8, 17, 97, 4096):
                    ref = self.fixture(counter, control); cpu = self.fixture(counter, control)
                    history = deque(maxlen=32); expected = deque(maxlen=32)
                    done = Accelerator._empty_i2c_poll(cpu, budget, history, ())
                    if not done:continue
                    for _ in range(done):
                        ref.step(); expected.append((*ref.last_instruction, None))
                    self.assertEqual(cpu.snapshot(), ref.snapshot())
                    self.assertEqual((cpu.bus.ticks, cpu.bus.activity, cpu.bus.events),
                                     (ref.bus.ticks, ref.bus.activity, ref.bus.events))
                    self.assertEqual(history, expected)
                    self.assertEqual(cpu.bus.i2c.snapshot(), ref.bus.i2c.snapshot())

    def test_terminal_iteration_breakpoints_and_changed_instructions_fall_back(self):
        cpu = self.fixture(1, 10)
        self.assertEqual(Accelerator._empty_i2c_poll(cpu, 100, deque(maxlen=32), ()), 0)

        cpu = self.fixture(100, 8)  # DONE without NACK must take success branch.
        self.assertEqual(Accelerator._empty_i2c_poll(cpu, 100, deque(maxlen=32), ()), 0)
        for address in range(0x100000, 0x100010, 2):
            cpu = self.fixture(100, 10)
            self.assertEqual(Accelerator._empty_i2c_poll(cpu, 100, deque(maxlen=32), (address,)), 0)
        cpu = self.fixture(100, 10); cpu.bus.write(0x100008, 0x1200, 2)
        self.assertEqual(Accelerator._empty_i2c_poll(cpu, 100, deque(maxlen=32), ()), 0)
        cpu = self.fixture(100, 10); cpu.regs[2] += 1
        self.assertEqual(Accelerator._empty_i2c_poll(cpu, 100, deque(maxlen=32), ()), 0)

    def test_batched_wait_stays_before_the_enabled_timer_interrupt_deadline(self):
        def timed():
            cpu = self.fixture(10000, 10); bus = cpu.bus
            bus.interrupt_masks = InterruptMasks(full=True)
            for i in range(16):
                bus.interrupt_masks.registers[0x60+i*4] = int.from_bytes(bytes(range(i*4, i*4+4)), 'little')
            bus.interrupt_masks.registers[0x40] = 1 << 10
            bus.timer_interrupts = True
            bus.timers = Timers(lambda: bus.ticks, input_hz=1000000)
            cpu.cr[0] = 0x80000140
            bus.timers.access(0x20, 4, 3); bus.timers.access(0x28, 4, 0xffffff00)
            bus.timers.access(0x10, 4, 2)
            bus.timers.channels[0]['count'] = 0xffffff00
            return cpu
        cpu, ref = timed(), timed(); accelerator = Accelerator(cpu)
        if accelerator.native is None:self.skipTest('native interpreter unavailable')
        due = cpu.interrupt_budget(1000)
        done = accelerator.advance(cpu, 1000, deque(maxlen=32))
        self.assertGreater(done, 0); self.assertLessEqual(done, due)
        for _ in range(done):ref.step()
        self.assertEqual(cpu.snapshot(), ref.snapshot())
        self.assertEqual(cpu.bus.timers.snapshot(), ref.bus.timers.snapshot())



if __name__ == '__main__':unittest.main()
