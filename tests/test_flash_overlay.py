"""Volatile virtual flash writes preserve source images and obey NOR constraints."""
import unittest
from starsat_emu.spi import SerialNOR
from starsat_emu.memory import EmulationStop


def command(flash, data):
    flash.select(True)
    result = bytes(flash.exchange(byte) for byte in data)
    flash.select(False)
    return result


class OverlayTests(unittest.TestCase):
    def test_program_wrap_bits_erase_and_write_enable_latch(self):
        flash = SerialNOR(b'\xff' * 8192, capacity=8192, writable=True)
        original = flash.data
        with self.assertRaises(EmulationStop):command(flash, b'\x02\0\0\0\x11')
        flash.select(False)
        command(flash, b'\x06')
        self.assertEqual(command(flash, b'\x05\0')[-1], 2)
        command(flash, b'\x02\0\0\xff\xaa\x55')
        self.assertFalse(flash.write_enable)
        self.assertEqual(command(flash, b'\x03\0\0\xff\0')[-1], 0xaa)
        self.assertEqual(command(flash, b'\x03\0\0\0\0')[-1], 0x55)
        command(flash, b'\x06');command(flash, b'\x02\0\0\0\xf0')
        self.assertEqual(command(flash, b'\x03\0\0\0\0')[-1], 0x50)
        command(flash, b'\x06');command(flash, b'\x20\0\0\0')
        self.assertEqual(command(flash, b'\x03\0\0\0\0')[-1], 255)
        self.assertEqual(flash.data, original)
        self.assertEqual(flash.program_operations, 2)
        self.assertEqual(flash.erase_operations, 1)
        self.assertFalse(SerialNOR(original).overlay)

    def test_read_only_instances_reject_write_enable_and_security_mutations(self):
        flash = SerialNOR(b'\xff' * 4096)
        for opcode in (6, 2, 0x20, 0xd8, 0x42, 0x44):
            flash.select(False);flash.select(True)
            with self.assertRaises(EmulationStop):flash.exchange(opcode)
        self.assertFalse(flash.overlay)
        self.assertFalse(flash.write_enable)

    def test_64k_block_erase_is_aligned_volatile_and_requires_write_enable(self):
        flash = SerialNOR(bytes(3 * 65536), capacity=3 * 65536, writable=True)
        original = flash.data
        with self.assertRaises(EmulationStop):command(flash, b'\xd8\x01\x23\x45')
        flash.select(False); command(flash, b'\x06')
        command(flash, b'\xd8\x01\x23\x45')
        self.assertEqual(len(flash.overlay), 65536)
        self.assertEqual(command(flash, b'\x03\x00\xff\xff\0\0')[-2:], b'\0\xff')
        self.assertEqual(command(flash, b'\x03\x01\xff\xff\0\0')[-2:], b'\xff\0')
        self.assertEqual(flash.data, original)
        self.assertFalse(flash.write_enable)
        self.assertEqual(flash.erase_operations, 1)


if __name__ == '__main__':unittest.main()
