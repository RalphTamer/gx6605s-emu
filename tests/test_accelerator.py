"""Differential checks against cpu.py, including unsupported boundaries."""
from collections import deque
import random
import unittest

from starsat_emu.accelerator import Accelerator
from starsat_emu.cpu import CPU
from starsat_emu.memory import Bus, EmulationStop


class AcceleratorTests(unittest.TestCase):
    def pair(self):
        left, right = Bus(), Bus()
        left.map("ram", 0x100000, 0x2000)
        right.map("ram", 0x100000, 0x2000)
        reference, native = CPU(left, 0x100000), CPU(right, 0x100000)
        accelerator = Accelerator(native)
        if accelerator.native is None:
            self.skipTest("build native/csky_fast.c with tools/build_macos.sh to enable")
        return reference, native, accelerator

    def test_all_encodings_with_both_carry_states(self):
        reference, native, accelerator = self.pair()
        rng, history = random.Random(4040), deque(maxlen=32)
        initial = rng.randbytes(0x2000)
        left, right = reference.bus.regions[0].data, native.bus.regions[0].data
        executed = 0
        for word in range(65536):
            for carry in (0, 1):
                left[:] = right[:] = initial
                left[:2] = right[:2] = word.to_bytes(2, "little")
                registers = [rng.getrandbits(32) if i % 2 else 0x100400 + rng.randrange(256) * 4 for i in range(16)]
                reference.pc = native.pc = 0x100000
                reference.c = native.c = carry
                reference.regs[:] = native.regs[:] = registers
                if not accelerator.advance(native, 1, history):
                    continue
                try:
                    reference.step()
                except EmulationStop as error:
                    self.fail(f"native executed {word:04x} while reference stopped: {error}")
                self.assertEqual((native.pc, native.c, native.regs, right),
                                 (reference.pc, reference.c, reference.regs, left), f"word={word:04x} carry={carry}")
                executed += 1
        self.assertGreater(executed, 80000)

    def test_mmio_boundary_and_last_instructions(self):
        reference, native, accelerator = self.pair()
        # movi r1, 2; addi r1, 1; st.w r1, (r2, 0). Store is MMIO.
        program = bytes.fromhex("216001200292")
        for cpu in (reference, native):
            cpu.bus.regions[0].data[:len(program)] = program
            cpu.regs[2] = 0x00400008
        history = deque(maxlen=32)
        self.assertEqual(accelerator.advance(native, 100, history), 2)
        self.assertEqual(native.pc, 0x100004)
        self.assertEqual(native.bus.uart.tx, b"")
        native.step()
        for _ in range(3): reference.step()
        self.assertEqual(native.snapshot(), reference.snapshot())
        self.assertEqual(native.bus.uart.tx, reference.bus.uart.tx)
        self.assertEqual([entry[0] for entry in history], [0x100000, 0x100002])

    def test_direct_segments_obey_permissions(self):
        _, native, accelerator = self.pair()
        native.bus.regions[0].data[:2] = bytes.fromhex("2160")
        native.cr[18] = 1
        native.pc = 0x80100000
        history = deque(maxlen=32)
        self.assertEqual(accelerator.advance(native, 1, history), 1)
        self.assertEqual(native.regs[1], 2)
        native.pc = 0x80100000
        native.mmu.registers[30] = 0
        self.assertEqual(accelerator.advance(native, 1, history), 0)
        native.mmu.registers[30] = 0x1e
        native.cr[0] = 0
        self.assertEqual(accelerator.advance(native, 1, history), 0)

    def test_breakpoint_stops_before_guest_instruction(self):
        _, native, accelerator = self.pair()
        native.bus.regions[0].data[:6] = bytes.fromhex("216001200120")
        history = deque(maxlen=32)
        self.assertEqual(accelerator.advance(native, 100, history, [0x100004]), 2)
        self.assertEqual(native.pc, 0x100004)
        self.assertEqual(native.regs[1], 3)
