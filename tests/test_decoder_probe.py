"""Bounds and result validation for offline decoder hypotheses."""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tools.probe_decoder import default_candidates, load_candidates, valid_romfs, WINDOW


class DecoderProbeTests(unittest.TestCase):
    def romfs(self):
        image = bytearray(b"-rom1fs-" + struct.pack(">II", 64, 0))
        image.extend(b"fixture\0".ljust(16, b"\0"))
        image.extend(struct.pack(">IIII", 1, 0, 0, 0))
        image.extend(b".\0".ljust(16, b"\0"))
        checksum = -sum(struct.unpack(">16I", image)) & 0xffffffff
        struct.pack_into(">I", image, 12, checksum)
        return image

    def test_valid_romfs_and_corrupted_checksum(self):
        image = self.romfs()
        self.assertTrue(valid_romfs(image))
        image[22] ^= 1
        self.assertFalse(valid_romfs(image))

    def test_small_size_is_not_validation(self):
        self.assertFalse(valid_romfs(bytes(64)))
        image = self.romfs()
        struct.pack_into(">I", image, 8, 1000000)
        self.assertFalse(valid_romfs(image))
        self.assertFalse(valid_romfs(image[:16]))

    def test_candidates_are_explicit_complete_and_do_not_mutate_writes(self):
        written = {0x140: 0x02030400}
        candidates = default_candidates(written)
        self.assertEqual(len(candidates), 9)
        self.assertTrue(all(set(c["registers"]) == set(WINDOW) for c in candidates))
        self.assertEqual(written, {0x140: 0x02030400})

    def test_candidate_input_rejects_partial_and_outside_registers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidates.json"
            for registers in ({"0x04800140": 0}, {"0x00400008": 0}, {"0x04800140": True}):
                path.write_text(json.dumps([{"name": "invalid", "registers": registers}]))
                with self.assertRaises(ValueError): load_candidates(path)
            registers = {hex(0x04800000 + offset): "0x12345678" for offset in WINDOW}
            path.write_text(json.dumps([{"name": "synthetic", "registers": registers}]))
            self.assertEqual(load_candidates(path)[0]["registers"][0x150], 0x12345678)
