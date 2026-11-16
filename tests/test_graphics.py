"""Bounded command DMA plus independently checked RAM-probe transport."""
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import serial_graphics_probe as probe
from starsat_emu.memory import Bus, EmulationStop
from starsat_emu.graphics import GraphicsAccelerator


class GraphicsTests(unittest.TestCase):
    def fixture(self):
        bus=Bus();bus.map('scratch',0x10000000,0x500000)
        ga=GraphicsAccelerator(bus)
        ga.access(0x10,4,1)
        ga.access(0x1c,4,0x101)
        for address,words in [(probe.SRC,probe.SOURCE),(probe.DST-32,probe.INITIAL),(probe.CMD,probe.COMMAND)]:
            for i,value in enumerate(words):bus.write(address+4*i,value)
        ga.access(4,4,probe.CMD);ga.access(8,4,80);ga.access(0x18,4,1)
        return bus,ga

    def test_dma_copies_rows_preserves_padding_and_asserts_only_after_execution(self):
        bus,ga=self.fixture();self.assertFalse(ga.irq_pending())
        ga.access(0x18,4,0x10001)
        self.assertTrue(ga.irq_pending());self.assertEqual(ga.bytes_written,48)
        for row in range(3):
            self.assertEqual([bus.read(probe.DST+row*32+x*4) for x in range(4)],list(probe.SOURCE[:4]))
            self.assertEqual([bus.read(probe.DST+row*32+x*4) for x in range(4,8)],[0x2468ace0]*4)
        self.assertEqual(bus.read(probe.DST-4),0x13579bdf)
        ga.access(0x1c,4,0x103);self.assertFalse(ga.irq_pending())

    def test_mmio_destination_and_unknown_operations_never_complete(self):
        for word,value in [(14,0x403008),(0,0x90400706),(2,0),(3,0x3c29807),(4,0)]:
            bus,ga=self.fixture();bus.write(probe.CMD+word*4,value)
            with self.assertRaises(EmulationStop):ga.access(0x18,4,0x10001)
            self.assertFalse(ga.irq_pending());self.assertEqual(ga.bytes_written,0)
            self.assertEqual(bus.read(probe.DST),0xff102030)
            self.assertFalse(bus.uart.tx)

    def test_entire_list_validated_before_ram_mutation(self):
        bus,ga=self.fixture()
        for i,value in enumerate(probe.COMMAND):bus.write(probe.CMD+80+4*i,value)
        bus.write(probe.CMD+80,0x12345678);ga.access(8,4,160);ga.access(0x18,4,2)
        with self.assertRaises(EmulationStop):ga.access(0x18,4,0x10002)
        self.assertEqual(bus.read(probe.DST),0xff102030);self.assertFalse(ga.irq_pending())

    def run_probe(self,stalled=False):
        code,idle=probe.program(2 if stalled else 1000000);bus=probe.ProbeBus(stalled)
        bus.map('code',probe.ram.ENTRY & 0x1fffffff,len(code),code)
        bus.map('scratch',0x10000000,0x500000);bus.outgoing.extend(b'O')
        cpu=probe.ram.CPU(bus,probe.ram.ENTRY);cpu.cr[18]=0x7d
        while cpu.steps<100000 and cpu.pc!=idle:cpu.step()
        self.assertEqual(cpu.pc,idle)
        return bytes(bus.outgoing)

    def test_probe_timeout_and_corrupted_frames_are_reported(self):
        frame=self.run_probe();result=probe.parse_reply(frame)
        self.assertTrue(result['copy_matches']);self.assertTrue(result['gate_restored'])
        self.assertFalse(result['timed_out'])
        timeout=probe.parse_reply(self.run_probe(True));self.assertTrue(timeout['timed_out'])
        self.assertFalse(timeout['copy_matches']);self.assertTrue(timeout['guards_and_padding_unchanged'])
        for size in range(len(frame)):self.assertIsNone(probe.parse_reply(frame[:size]))
        for offset in (1,18,50,120,len(frame)-1):
            damaged=bytearray(frame);damaged[offset]^=1
            with self.assertRaises(ValueError):probe.parse_reply(damaged)


if __name__=='__main__':unittest.main()
