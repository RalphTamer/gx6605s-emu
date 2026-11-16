"""Synthetic DMA fixtures verify scanout independently of receiver startup."""
import base64
import struct
import unittest
import zlib

from starsat_emu.memory import Bus, VideoRegisters, EmulationStop
from starsat_emu.video import OSDRenderer


class VideoTests(unittest.TestCase):
    def fixture(self):
        bus = Bus()
        bus.map("vram", 0x10000000, 0x4000)
        bus.video = VideoRegisters()
        bus.video.registers.update({0x90: 1, 0x94: 0x10000000, 0xe8: 3 << 20})
        return bus

    def descriptor(self, bus, address, x, width, pixels, end=True, next_head=0):
        words = [6 << 10, 0, x | ((x + width - 1) << 16), 0, pixels, next_head,
                 width | (0x80000000 if end else 0)]
        data = struct.pack("<7I", *words)
        for i, byte in enumerate(data): bus.write(address + i, byte, 1)

    def pixels(self, state):
        png = base64.b64decode(state["png_base64"])
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        compressed, offset = bytearray(), 8
        while offset < len(png):
            size = int.from_bytes(png[offset:offset + 4], "big")
            name = png[offset + 4:offset + 8]
            body = png[offset + 8:offset + 8 + size]
            self.assertEqual(zlib.crc32(name + body), int.from_bytes(png[offset + 8 + size:offset + 12 + size], "big"))
            if name == b"IDAT": compressed.extend(body)
            offset += size + 12
        return zlib.decompress(compressed)

    def test_guest_rgb565_pixels_and_frame_invalidation(self):
        bus = self.fixture()
        self.descriptor(bus, 0x10000000, 0, 2, 0x10001000)
        bus.write(0x10001000, 0x07e0f800, 4)
        renderer = OSDRenderer()
        first = renderer.capture(bus, True)
        self.assertEqual((first["width"], first["height"]), (2, 1))
        self.assertEqual(self.pixels(first), b"\0\xff\0\0\xff\0\xff\0\xff")
        self.assertNotIn("png_base64", renderer.capture(bus))
        bus.write(0x10001000, 0x001f, 2)
        second = renderer.capture(bus, True)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.pixels(second)[1:5], b"\0\0\xff\xff")

    def test_multiple_regions_expand_inferred_canvas(self):
        bus = self.fixture()
        self.descriptor(bus, 0x10000000, 0, 1, 0x10001000, False, 0x10000020)
        self.descriptor(bus, 0x10000020, 3, 1, 0x10001000)
        bus.write(0x10001000, 0xffff, 2)
        frame = OSDRenderer().capture(bus, True)
        self.assertEqual((frame["width"], frame["regions"]), (4, 2))
        self.assertEqual(self.pixels(frame), b"\0" + b"\xff" * 4 + b"\0" * 8 + b"\xff" * 4)

    def test_upscaling_uses_guest_pixels_and_reports_approximation(self):
        bus = self.fixture()
        self.descriptor(bus, 0x10000000, 0, 2, 0x10001000)
        bus.write(0x10001000, 0x07e0f800, 4)
        bus.video.registers.update({0x98: (2 << 16) | 4, 0x9c: 0x08000800})
        frame = OSDRenderer().capture(bus, True)
        self.assertEqual((frame['width'], frame['height']), (4, 2))
        self.assertIn('approximation', frame['scaling'])
        row = b'\0' + b'\xff\0\0\xff' * 2 + b'\0\xff\0\xff' * 2
        self.assertEqual(self.pixels(frame), row * 2)

    def test_invalid_dma_is_reported_without_touching_mmio(self):
        bus = self.fixture()
        self.descriptor(bus, 0x10000000, 0, 1, 0x00400008)
        frame = OSDRenderer().capture(bus, True)
        self.assertEqual(frame["status"], "unsupported")
        self.assertIn("outside RAM", frame["detail"])
        self.assertNotIn("png_base64", frame)

    def test_disabled_layer_produces_no_frame(self):
        bus = self.fixture()
        bus.video.registers[0x90] = 0
        self.assertEqual(OSDRenderer().capture(bus)["status"], "disabled")

    def test_payload_decoder_registers_require_explicit_values(self):
        video = VideoRegisters()
        with self.assertRaises(EmulationStop) as error:
            video.access(0x150, 4)
        self.assertEqual(error.exception.kind, "unknown-device-reset")
        video.registers[0x150] = 0x12345678
        self.assertEqual(video.access(0x150, 4), 0x12345678)

    def test_descriptor_byte_order_high_bit_is_sys_para_bit_28(self):
        bus = self.fixture()
        self.descriptor(bus, 0x10000000, 0, 2, 0x10001000)
        data = bus.regions[0].data
        # The 16-bpp driver's CDAB_GHEF order swaps descriptor halfwords.
        for offset in range(0, 28, 4):
            data[offset:offset + 4] = data[offset + 2:offset + 4] + data[offset:offset + 2]
        bus.video.registers[0xe8] = 1 << 28
        bus.write(0x10001000, 0x07e0f800, 4)
        frame = OSDRenderer().capture(bus, True)
        self.assertEqual(frame["status"], "ready")
        self.assertEqual(self.pixels(frame), b"\0\xff\0\0\xff\0\xff\0\xff")

    def test_sys_para_bit_22_does_not_change_descriptor_byte_order(self):
        bus = self.fixture()
        self.descriptor(bus, 0x10000000, 0, 1, 0x10001000)
        bus.video.registers[0xe8] |= 1 << 22
        self.assertEqual(OSDRenderer().capture(bus)["status"], "ready")
