import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from starsat_emu.cpu import CPU, decode
from starsat_emu.firmware import Firmware
from starsat_emu.memory import Bus, EmulationStop
from starsat_emu.__main__ import main


def machine(*words):
    bus = Bus()
    bus.map("code", 0x100000, 0x1000, struct.pack("<" + "H" * len(words), *words))
    return CPU(bus, 0x100000)


class InstructionTests(unittest.TestCase):
    def test_arithmetic_wraps_and_signed_compare(self):
        c = machine(0x2c01, 0x2001, 0x2401, 0x2201)
        for _ in range(4): c.step()
        self.assertEqual(c.regs[1], 0xffffffff)
        self.assertEqual(c.c, 1)  # -1 < 1

    def test_literal_pool_aligns_pc_plus_two(self):
        c = machine(0x7101, 0x7201, 0, 0, 0xbeef, 0x1234)
        c.step()  # (pc+2+4)&~3 = base+4
        self.assertEqual(c.regs[1], 0)
        c.step()  # (pc+2+4)&~3 = base+8
        self.assertEqual(c.regs[2], 0x1234beef)

    def test_backwards_branch(self):
        c = machine(0x6001, 0x2001, 0xf7fe)
        for _ in range(7): c.step()
        self.assertEqual(c.regs[1], 3)
        self.assertEqual(c.pc, 0x100002)

    def test_call_and_return(self):
        c = machine(0xf801, 0, 0x6052, 0x00cf)
        c.step()
        self.assertEqual(c.regs[15], 0x100002)
        c.step(); c.step()
        self.assertEqual(c.regs[2], 5)
        self.assertEqual(c.pc, 0x100002)

    def test_conditional_branches_use_carry(self):
        c = machine(0x6011, 0x2a01, 0xe001, 0x6001, 0x6022)
        for _ in range(4): c.step()
        self.assertEqual(c.regs[1], 1)
        self.assertEqual(c.regs[2], 2)

    def test_little_endian_store_load(self):
        c = machine(0x9231, 0xc461, 0xa5d1)
        c.regs[1], c.regs[2] = 0x100100, 0x1234abcd
        for _ in range(3): c.step()
        self.assertEqual(c.bus.read(0x10010c), 0x1234abcd)
        self.assertEqual(c.regs[4], 0xabcd)  # halfword displacement 6 * 2
        self.assertEqual(c.regs[5], 0xab)  # byte displacement 13

    def test_byte_store_truncates(self):
        c = machine(0xb201, 0xa301)
        c.regs[1], c.regs[2] = 0x100100, 0x1234abcd
        c.step(); c.step()
        self.assertEqual(c.regs[3], 0xcd)

    def test_shift_sign_and_carry(self):
        c = machine(0x3a01, 0x3c01, 0x3e01)
        c.regs[1] = 0x80000001
        c.step()
        self.assertEqual((c.regs[1], c.c), (0xc0000000, 1))
        c.step()
        self.assertEqual((c.regs[1], c.c), (0x80000000, 1))
        c.step()
        self.assertEqual((c.regs[1], c.c), (0x40000000, 0))

    def test_register_bank_switch(self):
        c = machine(0x11f8, 0x6031, 0x11f0)
        c.regs[1] = 99
        for _ in range(3): c.step()
        self.assertEqual(c.regs[1], 99)
        self.assertEqual(c.alternate[1], 3)

    def test_vector_base_alignment(self):
        c = machine(0x1811)
        c.regs[1] = 0x12345fff
        c.step()
        self.assertEqual(c.cr[1], 0x12345c00)

    def test_mmu_enable_requires_a_real_translation(self):
        c = machine(0x1921)
        c.regs[1] = 1
        c.step()
        with self.assertRaises(EmulationStop) as error: c.step()
        self.assertEqual(error.exception.kind, "mmu-miss")
        self.assertEqual((c.cr[18], c.pc, c.steps), (1, 0x100002, 1))

    def test_unsupported_opcode_stops(self):
        c = machine(0x4000)
        with self.assertRaises(EmulationStop) as error: c.step()
        self.assertEqual(error.exception.kind, "unsupported-instruction")
        self.assertEqual(c.steps, 0)

    def test_coprocessor_is_not_silently_ignored(self):
        c = machine(0x0011, 0x6f42)
        c.step()
        with self.assertRaises(EmulationStop) as error: c.step()
        self.assertEqual(c.cp, 1)
        self.assertEqual(error.exception.kind, "unsupported-coprocessor")

    def test_decode_branch_extremes(self):
        self.assertEqual(decode(0xf3ff, 0x100000).args, (0x100800,))
        self.assertEqual(decode(0xf400, 0x100000).args, (0xff802,))


class DeviceTests(unittest.TestCase):
    def test_unknown_hardware_fails(self):
        b = Bus()
        with self.assertRaises(EmulationStop): b.read(0x30a170)
        self.assertEqual(b.events[-1]["address"], "0x0030a170")

    def test_uart_polling_receive_transmit(self):
        b = Bus()
        self.assertEqual(b.read(0x400004), 0x80)
        b.uart.rx.append(65)
        self.assertEqual(b.read(0x400004), 0xa0)
        self.assertEqual(b.read(0x400008), 65)
        self.assertEqual(b.read(0x400004), 0x80)
        b.write(0x400008, 66)
        self.assertEqual(b.uart.tx, b"B")

    def test_guest_program_emits_uart_byte(self):
        c = machine(0x6482, 0x7102, 0xb201, 0x0000, 0, 0, 8, 0x40)
        for _ in range(3): c.step()
        self.assertEqual(c.bus.uart.tx, b"H")

    def test_interrupt_mask_latches_and_status_fails(self):
        b = Bus()
        b.write(0x500050, 0xffffffff)
        self.assertEqual(b.read(0x500050), 0xffffffff)
        with self.assertRaises(EmulationStop): b.read(0x500000)

    def test_clock_configuration_readback_without_inventing_reset(self):
        b = Bus()
        with self.assertRaises(EmulationStop) as error: b.read(0x30a178)
        self.assertEqual(error.exception.kind, "unknown-device-reset")
        b.write(0x30a170, 0x03000000)
        self.assertEqual(b.read(0x30a170), 0x03000000)
        b.write(0x30a0c8, 0x4001)
        self.assertEqual(b.read(0x30a0c8), 0x4001)
        b.write(0x30a0c8, 1)
        self.assertEqual(b.read(0x30a0c8), 1)
        with self.assertRaises(EmulationStop): b.write(0x30a184, 0)

    def test_rom_misalignment_and_overlap(self):
        b = Bus()
        b.map("rom", 0, 16, b"test", readonly=True)
        with self.assertRaises(EmulationStop): b.write(0, 0)
        with self.assertRaises(EmulationStop): b.read(1, 4)
        with self.assertRaises(ValueError): b.map("overlap", 8, 16)

    def test_instruction_fetch_never_reads_uart(self):
        b = Bus()
        b.uart.rx.append(65)
        with self.assertRaises(EmulationStop): b.read(0x400008, 2, fetch=True)
        self.assertEqual(list(b.uart.rx), [65])


class CLITests(unittest.TestCase):
    def make_image(self, folder):
        data = bytearray(0x100 + 4 + 0x3000)
        data[0x100:0x104] = bytes.fromhex("aa55aa55")
        # Program emits H through the actual CPU/bus/UART path, then breaks.
        data[0x104:0x114] = struct.pack("<8H", 0x6482, 0x7102, 0xb201, 0, 0, 0, 8, 0x40)
        path = Path(folder) / "test.bin"
        path.write_bytes(data)
        return path

    def test_end_to_end_trace_report_and_immutable_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_image(temp)
            original = path.read_bytes()
            report, trace = Path(temp) / "state.json", Path(temp) / "trace.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["run", str(path), "--trace", str(trace), "--report", str(report)])
            self.assertEqual(code, 0)
            result = json.loads(report.read_text())
            self.assertEqual(result["uart_output_text"], "H")
            self.assertEqual(result["cpu"]["steps"], 3)
            records = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["status"], "stopped")
            self.assertEqual(original, path.read_bytes())

    def test_refuses_firmware_as_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_image(temp)
            before = path.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", str(path), "--trace", str(path)]), 2)
            self.assertEqual(path.read_bytes(), before)

    def test_instruction_budget_and_breakpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_image(temp)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", str(path), "--max-steps", "1"]), 3)
                self.assertEqual(main(["run", str(path), "--breakpoint", "0x100000"]), 0)

    def test_rejects_invalid_image(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.bin"
            path.write_bytes(b"bad")
            with self.assertRaises(ValueError): Firmware(path)


class RealFirmwareTests(unittest.TestCase):
    def test_boot_emits_R_then_stops_for_unknown_clock_reset(self):
        # Local integration only: proprietary firmware is not bundled with this code.
        path = Path(__file__).resolve().parents[1] / "SR-4040HD_VEGA_V3.93_14102026.bin"
        if not path.exists(): self.skipTest("local receiver image unavailable")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        fw = Firmware(path)
        self.assertTrue(fw.inspect()["cramfs"][0]["crc_valid"])
        cpu = fw.machine()
        with self.assertRaises(EmulationStop) as error:
            for _ in range(1000): cpu.step()
        self.assertEqual(error.exception.kind, "unknown-device-reset")
        self.assertEqual(cpu.steps, 265)
        self.assertEqual(cpu.pc, 0x100218)
        self.assertEqual(cpu.bus.events[-1]["address"], "0x0030a178")
        self.assertEqual(cpu.bus.uart.tx, b"R")
        self.assertEqual(cpu.bus.interrupt_masks.registers[0x50], 0xffffffff)
        self.assertEqual(cpu.bus.interrupt_masks.registers[0x54], 0xffffffff)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
