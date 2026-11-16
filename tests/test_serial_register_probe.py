"""Fixed MMIO probe: exact read set/order, no target writes, per-record CRCs."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import serial_register_probe as probe


def execute(addresses=probe.ADDRESSES):
    values = {address: (0x87654321 ^ address) for address in addresses}
    code, idle = probe.reader(addresses)
    bus = probe.RegisterBus(values)
    bus.outgoing.extend(b"O")
    bus.map("register-probe-ram", probe.ram.ENTRY & 0x1fffffff, len(code), code)
    cpu = probe.ram.CPU(bus, probe.ram.ENTRY)
    cpu.cr[18] = 0x7d
    while cpu.steps < 100000 and cpu.pc != idle:
        cpu.step()
    if cpu.pc != idle:
        raise AssertionError("register probe exceeded execution budget")
    return bus, values


class RegisterProbeTests(unittest.TestCase):
    def test_demod_preset_uses_only_its_fixed_read_set_and_cannot_take_arbitrary_addresses(self):
        for group in ('demod-id', 'kernel-reads'):
            addresses = probe.GROUPS[group]
            bus, values = execute(addresses)
            self.assertEqual(bus.target_reads, list(addresses))
            result = probe.parse_reply(bus.outgoing, addresses)
            self.assertTrue(result['complete'])
            self.assertEqual(len(result['values']), len(addresses))
            self.assertEqual(result['values']['0x04e3ffc0'], '0x%08x' % values[0x04e3ffc0])
            self.assertTrue(all(a == 0x403000 for a, _, _ in bus.device_writes))
        with self.assertRaises(ValueError):
            probe.reader((0xdead0000,))
    def test_machine_code_reads_only_fixed_targets_and_checksums_exact_values(self):
        bus, values = execute()
        result = probe.parse_reply(bus.outgoing)
        self.assertIsNotNone(result)
        self.assertEqual(result["values"], {"0x%08x" % a: "0x%08x" % v for a, v in values.items()})
        self.assertEqual(bus.target_reads, list(probe.ADDRESSES))
        self.assertTrue(all(address == 0x403000 for address, _, _ in bus.device_writes))
        self.assertEqual(result["pending_address"], None)

    def test_partial_capture_preserves_completed_records_and_names_next_read(self):
        bus, _ = execute()
        frame = bytes(bus.outgoing)
        for length in range(len(frame)):
            self.assertIsNone(probe.parse_reply(frame[:length]))
        partial = probe.observations(frame[:17 + probe.RECORD_SIZE + 8])
        self.assertEqual(list(partial["values"]), ["0x040036d4"])
        self.assertEqual(partial["pending_address"], "0x04800140")
        self.assertFalse(partial["complete"])

    def test_corrupted_marker_address_value_checksum_or_footer_is_rejected(self):
        bus, _ = execute()
        for offset in (1, 17, 21, 25, 29, 33, len(bus.outgoing) - 1):
            frame = bytearray(bus.outgoing)
            frame[offset] ^= 1
            with self.assertRaises(ValueError):
                probe.parse_reply(frame)
        with self.assertRaises(ValueError):
            probe.parse_reply(bus.outgoing + b"extra")


if __name__ == "__main__":
    unittest.main()
