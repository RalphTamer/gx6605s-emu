import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import queue
import unittest

from starsat_emu.cpu import CPU
from starsat_emu.memory import Bus, DRAMController, InterruptMasks, EmulationStop
from starsat_emu.mmu import MMU
from starsat_emu.otp import OTP
from starsat_emu.profile import MachineProfile
from starsat_emu.rtc import RTC
from starsat_emu.spi import SerialNOR, SPIController
from starsat_emu.firmware import Firmware
from starsat_emu.session import Session
from test_emulator import machine
import test_emulator as initial_tests


class ExtendedCPU(unittest.TestCase):
    def test_register_shift_widths_and_signed_arithmetic(self):
        for word, expected in [(0x1b21, 0), (0x0b21, 0), (0x1a21, 0xffffffff)]:
            c = machine(word)
            c.regs[1], c.regs[2] = 0x80000000, 32
            c.step()
            self.assertEqual(c.regs[1], expected)
        c = machine(0x1b21)
        c.regs[1], c.regs[2] = 7, 64
        c.step()
        self.assertEqual(c.regs[1], 7)  # shift count is low six bits

    def test_carry_arithmetic_and_borrow(self):
        c = machine(0x0621, 0x0721)
        c.regs[1], c.regs[2], c.c = 0xffffffff, 0, 1
        c.step()
        self.assertEqual((c.regs[1], c.c), (0, 1))
        c.regs[2], c.c = 1, 0
        c.step()
        self.assertEqual((c.regs[1], c.c), (0xfffffffe, 0))

    def test_signed_division_truncates_towards_zero(self):
        c = machine(0x3212)
        c.regs[1], c.regs[2] = 2, 0xfffffff9
        c.step()
        self.assertEqual(c.regs[2], 0xfffffffd)
        c = machine(0x2c12)
        with self.assertRaises(EmulationStop) as error: c.step()
        self.assertEqual(error.exception.kind, "divide-by-zero")

    def test_multi_register_stack_roundtrip(self):
        c = machine(0x007c, 0x006c)
        c.regs[0] = 0x100800
        c.regs[12:16] = [12, 13, 14, 15]
        c.step()
        c.regs[12:16] = [0] * 4
        c.step()
        self.assertEqual(c.regs[12:16], [12, 13, 14, 15])

    def test_decode_cache_does_not_hide_modified_code(self):
        c = machine(0x6011)
        c.step()
        c.pc = 0x100000
        c.bus.write(c.pc, 0x6021, 2)
        c.step()
        self.assertEqual(c.regs[1], 2)


class MMUTests(unittest.TestCase):
    def mapped(self, bits=12, asid=0):
        m = MMU()
        mask = ((1 << (bits + 1)) - 1) & ~0x1fff
        m.register(6, mask)
        m.register(4, asid)
        m.register(2, (0x10000000 >> 6) | 7)
        m.register(3, ((0x10000000 + (1 << bits)) >> 6) | 7)
        m.register(8, 1 << 29)
        return m

    def test_even_and_odd_pages_for_supported_sizes(self):
        for bits in (12, 14, 16, 18, 20, 22, 24):
            m = self.mapped(bits)
            self.assertEqual(m.translate(123), 0x1000007b)
            self.assertEqual(m.translate((1 << bits) + 16, write=True), 0x10000000 + (1 << bits) + 16)

    def test_direct_segments_alias_the_same_ram(self):
        c = machine(0x1200)
        c.bus.map("dram", 0x10000000, 0x1000)
        c.cr[18] = 1
        c.write(0x90000000, 0x12345678)
        self.assertEqual(c.read(0xb0000000), 0x12345678)
        with self.assertRaises(EmulationStop): c.mmu.translate(0x90000000, supervisor=False)

    def test_permissions_asid_and_invalidation(self):
        m = self.mapped(asid=1)
        m.register(2, (0x10000000 >> 6) | 2)  # valid, read-only, ASID-local
        m.register(3, (0x10001000 >> 6) | 2)
        m.register(8, 1 << 29)
        self.assertEqual(m.translate(0), 0x10000000)
        with self.assertRaises(EmulationStop): m.translate(0, write=True)
        m.register(4, 2)
        with self.assertRaises(EmulationStop): m.translate(0)
        m.register(4, 1)
        m.register(8, (1 << 27) | 1)
        with self.assertRaises(EmulationStop): m.translate(0)

    def test_all_invalidation_clears_cached_translations(self):
        m = self.mapped()
        m.translate(0)
        m.register(8, 1 << 26)
        self.assertFalse(m.entries)
        with self.assertRaises(EmulationStop): m.translate(0)

    def test_tlb_probe_and_readback(self):
        m = self.mapped()
        m.register(8, 1 << 31)
        self.assertEqual(m.register(0), 0)
        m.register(2, 0)
        m.register(8, 1 << 30)
        self.assertEqual(m.register(2), (0x10000000 >> 6) | 7)


class PeripheralTests(unittest.TestCase):
    def test_kernel_uart_configuration_extension_requires_write_before_read(self):
        bus = Bus()
        with self.assertRaises(EmulationStop):
            bus.read(0x400014)
        bus.write(0x400014, 1728)
        self.assertEqual(bus.read(0x400014), 1728)
        bus.write(0x400008, ord('K'))
        self.assertEqual(bus.uart.tx, b'K')
    def transfer(self, spi, data, selected=True):
        count = len(data)
        control = (0x30000 if selected else 0) | ((count - 1) << 14)
        spi.access(0, 4, control)
        spi.access(8, 4, int.from_bytes(data, "big"))
        spi.access(0, 4, control | 0x1000)
        self.assertEqual(spi.access(4, 4), 1)
        result = spi.access(12, 4).to_bytes(count, "big")
        spi.access(0, 4, control)
        return result

    def test_spi_read_across_fifo_transfers_and_chip_select(self):
        spi = SPIController(SerialNOR(b"ABCDEFGH", capacity=32))
        self.transfer(spi, b"\x03\x00\x00\x02")
        self.assertEqual(self.transfer(spi, b"\xff" * 4), b"CDEF")
        self.assertEqual(self.transfer(spi, b"\xff" * 2), b"GH")
        spi.access(0, 4, 0)
        self.transfer(spi, b"\x03\x00\x00\x00")
        self.assertEqual(self.transfer(spi, b"\xff"), b"A")

    def test_fast_read_dummy_and_jedec(self):
        flash = SerialNOR(b"AB", capacity=16)
        flash.select(True)
        for byte in b"\x0b\x00\x00\x01\x00": flash.exchange(byte)
        self.assertEqual(flash.exchange(0), ord("B"))
        flash.select(False); flash.select(True)
        flash.exchange(0x9f)
        self.assertEqual(bytes(flash.exchange(0) for _ in range(3)), bytes.fromhex("ef4016"))

    def test_native_kernel_jedec_sequence_latches_length_before_transfer(self):
        spi = SPIController(SerialNOR(b'', capacity=16))
        spi.access(0, 4, 0x30000 | (3 << 14))
        spi.access(8, 4, 0x9f000000)
        spi.access(0, 4, 0x30000)  # native kernel clocks only the queued 9F
        spi.access(0, 4, 0x31000)
        self.assertEqual(spi.flash.command, 0x9f)
        spi.access(0, 4, 0)
        spi.access(0, 4, 0x30000 | (3 << 14))
        spi.access(0, 4, 0x31000 | (3 << 14))
        self.assertEqual(spi.access(12, 4), 0xffef4016)

    def test_dual_read_keeps_partition_magic_aligned(self):
        spi = SPIController(SerialNOR(bytes.fromhex("aabcdefa05000000"), capacity=16))
        self.transfer(spi, bytes.fromhex("3b000000"))
        control = 0x30000 | (3 << 14) | SPIController.DUAL_RECEIVE
        spi.access(0, 4, control)
        spi.access(8, 4, 0xffffffff)
        spi.access(0, 4, control | SPIController.START)
        # Eight dummy clocks consume two dual-lane FIFO bytes.
        self.assertEqual(spi.access(12, 4), 0xffffaabc)
        spi.access(0, 4, control)
        spi.access(0, 4, control | SPIController.START)
        self.assertEqual(spi.access(12, 4), 0xdefa0500)
        self.assertEqual(spi.flash.read_bytes, 6)

    def test_gpio_direction_set_clear_and_external_inputs(self):
        from starsat_emu.gpio import GPIO
        gpio = GPIO(0xffffffff)
        gpio.access(0, 4, 3)
        self.assertEqual(gpio.access(4, 4), 0xfffffffc)
        gpio.access(8, 4, 5)
        self.assertEqual(gpio.access(4, 4), 0xfffffffd)
        gpio.access(12, 4, 1)
        self.assertEqual(gpio.access(4, 4), 0xfffffffc)
        gpio.access(0x1c, 4, 4)
        self.assertEqual(gpio.access(4, 4), 0xfffffff8)

    def test_otp_information_requires_explicit_profile_value(self):
        with self.assertRaises(EmulationStop): OTP().access(12, 4)
        self.assertEqual(OTP(info=0x1800).access(12, 4), 0x1800)
        with self.assertRaises(EmulationStop): OTP(info=0).access(12, 4, 7)

    def test_nor_rejects_programming(self):
        flash = SerialNOR(b"ABC")
        flash.select(True)
        with self.assertRaises(EmulationStop): flash.exchange(2)
        self.assertEqual(flash.data[:3], b"ABC")

    def test_dram_ready_requires_start(self):
        d = DRAMController()
        d.access(0xb8, 4, 0)
        self.assertEqual(d.access(0xb8, 4), 0)
        d.access(0, 4, 0x401)
        self.assertEqual(d.access(0xb8, 4), 0x20)
        d.access(0, 4, 0x400)
        self.assertEqual(d.access(0xb8, 4), 0)

    def test_rtc_progress_and_pause(self):
        ticks = [0]
        rtc = RTC(lambda: ticks[0])
        rtc.access(0, 4, 1)
        ticks[0] = 123456
        self.assertEqual(rtc.access(0x64, 4), 56)
        self.assertEqual(rtc.access(0x68, 4), 234)
        self.assertEqual(rtc.access(0x6c, 4), 1)
        rtc.access(0, 4, 0)
        ticks[0] += 10000
        self.assertEqual(rtc.access(0x6c, 4), 1)

    def test_otp_read_pulse_is_not_a_key_bypass(self):
        otp = OTP()
        self.assertEqual(otp.access(8, 4), 0x400)
        otp.access(0, 4, 0x4000 | (0x103 << 3))
        self.assertEqual(otp.access(8, 4), 0x600)
        otp.access(0, 4, 0)
        self.assertEqual(otp.reads, 1)
        otp.access(4, 4, 1)
        self.assertEqual(otp.access(4, 4), 1)
        with self.assertRaises(EmulationStop): otp.access(4, 4, 2)

    def test_interrupt_gate_set_clear(self):
        irq = InterruptMasks(full=True)
        irq.access(0x20, 4, 3)
        irq.access(0x30, 4, 1)
        self.assertEqual(irq.access(0x40, 4), 2)
        self.assertEqual(irq.access(0, 4), 0)


class ProfileAndSessionTests(unittest.TestCase):
    def test_exploratory_is_explicit_and_strict_stays_empty(self):
        strict = MachineProfile.load("strict")
        exploratory = MachineProfile.load("exploratory")
        self.assertEqual(strict.clock_reset, {})
        self.assertFalse(strict.spi_flash)
        self.assertTrue(exploratory.dram_controller)
        self.assertTrue(exploratory.spi_flash)
        self.assertGreater(len(exploratory.assumptions), 3)

    def test_malformed_profile_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            for raw in [[], {"name": "x", "description": "x", "spi_flash": "yes"},
                        {"name": "x", "description": "x", "clock_reset": {"0": 0}}]:
                path.write_text(json.dumps(raw))
                with self.assertRaises(ValueError): MachineProfile.load(str(path))

    def test_debugger_rejects_mmio_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = initial_tests.CLITests().make_image(folder)
            s = Session(Firmware(path), MachineProfile.load("strict"))
            self.assertEqual(s.memory(0x100000, 2), bytes.fromhex("8264"))
            with self.assertRaises(ValueError): s.memory(0x400008, 4)

    def test_profile_supplies_specific_vpu_reset_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"name": "fixture", "description": "synthetic test value",
                                        "video_registers": True, "video_reset": {"0x04800150": "0x12345678"}}))
            profile = MachineProfile.load(path)
            image = initial_tests.CLITests().make_image(folder)
            session = Session(Firmware(image), profile)
            self.assertEqual(session.cpu.bus.read(0x04800150), 0x12345678)
            self.assertEqual(profile.summary()["video_reset"]["0x4800150"], "0x12345678")

    def test_empty_uart_loop_yields_then_resumes_on_input(self):
        with tempfile.TemporaryDirectory() as folder:
            image = initial_tests.CLITests().make_image(folder)
            session = Session(Firmware(image), MachineProfile.load("strict"))
            # Load status, test RX_READY, loop until input, then halt.
            code = bytes.fromhex("047212830462430efcef0000")
            session.cpu.bus.regions[0].data[:len(code)] = code
            session.cpu.bus.write(0x100010, 0x00400000)
            stop = session.advance(10000)
            self.assertEqual(stop["reason"], "waiting-for-uart")
            self.assertLess(session.cpu.steps, 10000)
            session.cpu.bus.uart.rx.extend(b"x")
            self.assertEqual(session.advance(100)["reason"], "halt")

    def test_fetch_fault_is_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = initial_tests.CLITests().make_image(folder)
            s = Session(Firmware(path), MachineProfile.load("strict"))
            s.cpu.pc = 0x400000
            s.advance(1)
            self.assertEqual(s.snapshot()["recent_instructions"][-1]["instruction"], "instruction fetch")

    def test_subprocess_bridge_runs_steps_pauses_and_resets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = initial_tests.CLITests().make_image(folder)
            task = subprocess.Popen([sys.executable, "-u", "-m", "starsat_emu", "bridge", str(path), "--profile", "strict"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            def read():
                for line in task.stdout: lines.put(json.loads(line))
            threading.Thread(target=read, daemon=True).start()
            try:
                self.assertEqual(lines.get(timeout=5)["event"], "state")
                task.stdin.write('{"command":"run","count":20}\n'); task.stdin.flush()
                for _ in range(10):
                    event = lines.get(timeout=5)
                    if event.get("state", {}).get("reason") == "halt": break
                self.assertEqual(event["state"]["uart_output_text"], "H")
                task.stdin.write('{"command":"reset","profile":"strict"}\n'); task.stdin.flush()
                event = lines.get(timeout=5)
                self.assertEqual(event["state"]["cpu"]["steps"], 0)
                task.stdin.write('{"command":"step"}\n'); task.stdin.flush()
                event = lines.get(timeout=5)
                self.assertEqual(event["state"]["cpu"]["steps"], 1)
                task.stdin.write('{"command":"quit"}\n'); task.stdin.flush()
                self.assertEqual(task.wait(timeout=5), 0)
            finally:
                if task.poll() is None: task.kill(); task.wait()
                task.stdin.close(); task.stdout.close(); task.stderr.close()


class ExploratoryBootTests(unittest.TestCase):
    def test_real_firmware_loads_second_stage_from_virtual_spi(self):
        path = Path(__file__).resolve().parents[1] / "SR-4040HD_VEGA_V3.93_14102026.bin"
        if not path.exists():
            self.skipTest("local firmware is not bundled")
        original = path.read_bytes()
        session = Session(Firmware(path), MachineProfile.load("exploratory"))
        stop = session.advance(3000000)
        self.assertIsNone(stop)
        self.assertEqual(session.cpu.steps, 3000000)
        self.assertEqual(session.cpu.bus.uart.tx, b"RUN")
        self.assertTrue(session.cpu.bus.dram_controller.started)
        self.assertTrue(session.cpu.cr[18] & 1)
        self.assertGreater(session.cpu.bus.spi.flash.read_bytes, 93700)
        self.assertGreater(session.cpu.bus.spi.transfers, 23427)
        self.assertGreater(len(session.cpu.mmu.entries), 0)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
