"""Replay the native instruction-port transcript without inventing execution."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from starsat_emu.av_mcu import AVMicrocontroller
from starsat_emu.firmware import Firmware
from starsat_emu.memory import Bus, EmulationStop
from starsat_emu.profile import MachineProfile


class AVMicrocontrollerTests(unittest.TestCase):
    def test_native_uploads_keep_every_instruction_and_stop_at_execution(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('receiver', 'v393'):
            program = (root / 'analysis' / (name + '-av-microcode.bin')).read_bytes()
            device = AVMicrocontroller(0)
            bus = Bus(); bus.av_mcu = device; bus.map('dram', 0x10000000, 4096, b'unchanged RAM')
            before = bytes(bus.regions[0].data)
            self.assertEqual(bus.read(0x04200018), 0)
            bus.write(0x04200000, 0)
            for index, word in enumerate(struct.unpack('<2048H', program)):
                bus.write(0x04200004, index << 16 | word)
            for offset, value in ((0x100, 0x12633000), (0x104, 0x12651000),
                                  (0x108, 0x12678000), (0x160, 1), (0x58, 0x1e000)):
                bus.write(0x04200000 + offset, value)
            bus.write(0x04200000, 1)
            self.assertEqual(device.program_bytes(), program)
            state = device.snapshot()
            self.assertEqual(state['loaded_words'], 2048)
            self.assertEqual(state['program_sha256'], hashlib.sha256(program).hexdigest())
            with self.assertRaisesRegex(EmulationStop, 'instruction set/backend not implemented'):
                bus.read(0x04200160)
            self.assertEqual(device.registers[0x160], 1)
            self.assertEqual(bytes(bus.regions[0].data), before)
            device.access(0, 4, 0)
            with self.assertRaises(EmulationStop):device.access(0x18, 4)

    def test_incomplete_and_invalid_uploads_cannot_launch(self):
        device = AVMicrocontroller(0)
        for value in (0x8000000, 0xffffffff):
            with self.assertRaises(EmulationStop):device.access(4, 4, value)
        device.access(4, 4, 0xe40e)
        with self.assertRaisesRegex(EmulationStop, 'complete captured instruction upload'):
            device.access(0, 4, 1)
        self.assertFalse(device.started)
        with self.assertRaises(ValueError):device.program_bytes()
        self.assertIsNone(device.snapshot()['program_sha256'])

    def test_opaque_readback_never_returns_the_last_instruction_or_mailbox_write(self):
        device = AVMicrocontroller(0)
        device.access(4, 4, 0xe40e); device.access(0x160, 4, 1)
        for offset in (0, 4, 0x160, 0x100):
            with self.assertRaises(EmulationStop):device.access(offset, 4)
        for offset, size, value in ((4, 2, 0), (0x18, 4, 1), (0, 4, 2)):
            with self.assertRaises(EmulationStop):device.access(offset, size, value)

    def test_profile_requires_an_explicit_observation_and_checks_overlaps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'p.json'
            base = {'name': 'fixture', 'description': 'observed AV startup',
                    'av_microcontroller': {'boot_status': 0}}
            path.write_text(json.dumps(base)); profile = MachineProfile.load(path)
            firmware = Firmware.__new__(Firmware); firmware.payload = b'\0' * 64
            cpu = firmware.machine(load_size=32, profile=profile)
            self.assertEqual(cpu.bus.av_mcu.boot_status, 0)
            for invalid in (True, {}, {'boot_status': True}, {'boot_status': -1}, {'boot_status': 0, 'ready': True}):
                path.write_text(json.dumps({**base, 'av_microcontroller': invalid}))
                with self.assertRaises(ValueError):MachineProfile.load(path)
            path.write_text(json.dumps({**base, 'provisional_registers':
                {'0x04200160': {'name': 'overlap', 'reset': 0}}}))
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                firmware.machine(load_size=32, profile=MachineProfile.load(path))

    def captured_receiver(self):
        root = Path(__file__).resolve().parents[1]
        device = AVMicrocontroller(0, bootstrap=AVMicrocontroller.captured_bootstrap)
        bus = Bus(); bus.av_mcu = device
        bus.map('av-ram', 0x12633000, 0x90000,
                (root / 'traces/serial-av-startup-20261114T082509334521Z.receiver.ram.bin').read_bytes())
        program = (root / 'analysis/receiver-av-microcode.bin').read_bytes()
        for index, word in enumerate(struct.unpack('<2048H', program)):
            bus.write(0x04200004, index << 16 | word)
        for offset, value in ((0x104, 0x12651000), (0x108, 0x12678000),
                              (0x100, 0x12633000), (0x10c, 0), (0x110, 4),
                              (0x170, 0), (0x160, 1), (0x58, 0x1e000), (0, 1)):
            bus.write(0x04200000 + offset, value)
        return bus, device

    def test_physical_bootstrap_transition_preserves_every_ram_byte_and_irq(self):
        bus, device = self.captured_receiver()
        before = bytes(bus.regions[0].data)
        irq = dict(bus.interrupt_masks.registers)
        self.assertEqual(bus.read(0x04200160), 0)
        self.assertTrue(device.bootstrap_completed)
        self.assertEqual(bytes(bus.regions[0].data), before)
        self.assertEqual(bus.interrupt_masks.registers, irq)
        for offset in (0x20, 0x160, 0x164, 0x168, 0x170, 0x174):
            self.assertEqual(bus.read(device.base + offset), 0)
        for offset, value in ((0x160, 1), (0x164, 0), (0, 1), (4, 0)):
            with self.assertRaisesRegex(EmulationStop, 'further host commands'):
                bus.write(device.base + offset, value)
        # The trace model uses measured stopped status as representative idle
        # status; it does not claim a clocked instruction-PC implementation.
        self.assertEqual(bus.read(0x04200018), 0x8b)
        bus.write(0x04200000, 0)
        self.assertEqual(bus.read(0x04200018), 0x8b)

    def test_changed_program_layout_or_any_captured_ram_byte_cannot_complete(self):
        for change in ('iram', 'configuration', 'ram-start', 'ram-end', 'warm-start'):
            bus, device = self.captured_receiver()
            if change == 'iram':device.iram[0] ^= 1
            elif change == 'configuration':device.registers[0x58] = 0x3000
            elif change == 'ram-start':bus.regions[0].data[0] ^= 1
            elif change == 'ram-end':bus.regions[0].data[-1] ^= 1
            else:device.starts = 2
            with self.assertRaisesRegex(EmulationStop, 'differs from the physical receiver capture'):
                bus.read(0x04200160)
            self.assertEqual(device.registers[0x160], 1)
            self.assertFalse(device.bootstrap_completed)

    def test_vpu_command_model_handles_channel_open_version_seq_init_and_gdi_idle(self):
        root = Path(__file__).resolve().parents[1]
        device = AVMicrocontroller(0, bootstrap=AVMicrocontroller.captured_bootstrap, vpu_commands=True)
        bus = Bus(); bus.av_mcu = device
        bus.map('av-ram', 0x12633000, 0x90000,
                (root / 'traces/serial-av-startup-20261114T082509334521Z.receiver.ram.bin').read_bytes())
        program = (root / 'analysis/receiver-av-microcode.bin').read_bytes()
        for index, word in enumerate(struct.unpack('<2048H', program)):
            bus.write(0x04200004, index << 16 | word)
        for offset, value in ((0x104, 0x12651000), (0x108, 0x12678000),
                              (0x100, 0x12633000), (0x10c, 0), (0x110, 4),
                              (0x170, 0), (0x160, 1), (0x58, 0x1e000), (0, 1)):
            bus.write(0x04200000 + offset, value)
        self.assertEqual(bus.read(0x04200160), 0)
        bus.write(0x0420105c, 10)
        bus.write(0x04200120, 0x12000000)
        bus.write(0x04200124, 0x12000000)
        bus.write(0x04200160, 1)
        bus.write(0x04200164, 0x0f)  # FIRMWARE_GET
        self.assertEqual(bus.read(0x04200160), 0)
        self.assertEqual(bus.read(0x042001c0), 0x00013100)
        bus.write(0x04200160, 1)
        bus.write(0x04200164, 0x01)  # DEC_SEQ_INIT
        self.assertEqual(bus.read(0x04200160), 0)
        self.assertEqual(bus.read(0x04200174) & 1, 1)
        bus.write(0x042010f0, 0x11)
        self.assertEqual(bus.read(0x042010f4), 0x77)



    def test_runtime_profile_and_fixed_bits_provisional_register(self):
        profile = MachineProfile.load('receiver-runtime')
        self.assertTrue(profile.av_microcontroller['vpu_commands'])
        self.assertEqual(profile.provisional_registers[0x04600118]['fixed_bits'], 0x10)
        firmware = Firmware.__new__(Firmware); firmware.payload = b'\0' * 64
        cpu = firmware.machine(load_size=32, profile=profile)
        cpu.bus.write(0x04600118, 0)
        self.assertEqual(cpu.bus.read(0x04600118), 0x10)



    def test_infrared_nec_queue_and_irq4(self):
        from starsat_emu.memory import Infrared, InterruptMasks
        from starsat_emu.cpu import CPU
        bus = Bus()
        bus.interrupt_masks = InterruptMasks(full=True)
        bus.interrupt_masks.access(0x40, 4, 0xffffffff)
        bus.interrupt_masks.access(0x64, 4, 0x07060504)
        bus.infrared = Infrared()
        bus.infrared.access(0, 4, 8)
        bus.infrared.queue_nec(0x00ff0a55)
        self.assertEqual((bus.read(0x00204004) >> 3) & 0x3f, 33)
        self.assertEqual(bus.read(0x00204004) & 2, 2)
        cpu = CPU(bus, 0x100000)
        cpu.cr[0] |= 0x140
        self.assertTrue(cpu.interrupt_ready())
        leader = bus.read(0x00204008)
        self.assertEqual(leader & 0xffff, 9000)
        for _ in range(32):
            bus.read(0x00204008)
        bus.write(0x00204004, 2)
        self.assertFalse(bus.infrared.irq_pending())


if __name__ == '__main__':unittest.main()
