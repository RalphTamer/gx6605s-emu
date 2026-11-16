# SPDX-License-Identifier: MIT
# Register/descriptor layout reference: Copyright (c) 2021 Sanpe <sanpeqf@gmail.com>.
"""Software scanout of the guest's GX6605S OSD region list.

Descriptor and color layouts follow xboot's gx6605s-osd.c / gx6605s-vpu.h.
This renders guest memory only; it does not invent receiver menus or decode
MPEG video. Upscaling uses a nearest-neighbor approximation; hardware filter
phases, interlacing and video-picture layers remain unimplemented.
"""
import base64
import hashlib
import struct
import zlib


class VideoFault(ValueError):
    pass


def ram_bytes(bus, address, size):
    if address < 0 or size < 0 or address + size > 0x100000000:
        raise VideoFault("invalid OSD DMA address/size")
    for region in bus.regions:
        offset = address - region.base
        if 0 <= offset and offset + size <= len(region.data):
            return bytes(region.data[offset:offset + size])
    raise VideoFault(f"OSD DMA points outside RAM: 0x{address:08x}")


def png_rgba(width, height, rgba):
    def chunk(name, content):
        return struct.pack(">I", len(content)) + name + content + struct.pack(">I", zlib.crc32(name + content))
    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def expand(value, bits):
    return value * 255 // ((1 << bits) - 1)


class OSDRenderer:
    def __init__(self):
        self.key = None
        self.cached = None

    def capture(self, bus, include_pixels=False):
        device = bus.video
        if device is None or not device.registers.get(0x90, 0) & 1:
            return {"status": "disabled", "detail": "Guest OSD layer is not enabled"}
        try:
            result = self._capture(bus, device.registers)
        except VideoFault as error:
            return {"status": "unsupported", "detail": str(error)}
        result = dict(result)
        png = result.pop("png")
        if include_pixels:
            result["png_base64"] = base64.b64encode(png).decode("ascii")
        return result

    def _capture(self, bus, registers):
        head = registers.get(0x94, 0)
        if not head or head & 3:
            raise VideoFault("OSD first region pointer is unset or misaligned")
        parameter = registers.get(0xe8, 0)
        # NationalChip's fbdev driver splits the 3-bit byte sequence between
        # SYS_PARA bits 21:20 and bit 28; bit 22 is not the high order bit.
        order = ((parameter >> 20) & 3) | ((parameter >> 26) & 4)
        if order not in (0, 3, 4):
            raise VideoFault(f"OSD byte order {order} is not supported")
        zoom = registers.get(0x9c, 0x10001000)
        hzoom, vzoom = (zoom & 65535, zoom >> 16) if zoom else (4096, 4096)
        scaled = (hzoom, vzoom) != (4096, 4096)
        if not 0 < hzoom <= 4096 or not 0 < vzoom <= 4096:
            raise VideoFault('OSD downscaling or zero zoom is not implemented')
        x_offset = registers.get(0xac, 0) & 1023
        y_offset = (registers.get(0xac, 0) >> 16) & 1023
        view = registers.get(0x98, 0)
        width, height = view & 2047, (view >> 16) & 2047
        output_width, output_height = width, height
        if scaled:
            if not (0 < width <= 1920 and 0 < height <= 1080):
                raise VideoFault('scaled OSD requires a bounded explicit output view')
            width = ((width - 1) * hzoom >> 12) + 1
            height = ((height - 1) * vzoom >> 12) + 1
        infer_width, infer_height = not width, not height
        dma_bytes = 0
        digest = hashlib.sha256(repr(sorted(registers.items())).encode())
        regions, visited = [], set()
        for _ in range(16):
            if head in visited:
                raise VideoFault("cyclic OSD list without LIST_END")
            visited.add(head)
            raw = ram_bytes(bus, head, 28)
            digest.update(raw)
            words = list(struct.unpack("<7I", raw))
            if order == 0:
                words = [int.from_bytes(v.to_bytes(4, "little"), "big") for v in words]
            elif order == 4:
                words = [((v & 65535) << 16) | (v >> 16) for v in words]
            control, palette_address, horizontal, vertical, pixels, next_head, alpha = words
            left, right = horizontal & 2047, (horizontal >> 16) & 2047
            top, bottom = vertical & 2047, (vertical >> 16) & 2047
            rw, rh = right - left + 1, bottom - top + 1
            stride = alpha & 8191
            if not (0 < rw <= 1920 and 0 < rh <= 1080 and rw <= stride <= 8191):
                raise VideoFault("invalid OSD region dimensions/stride")
            fmt, true_color = (control >> 10) & 7, (control >> 24) & 3
            if fmt == 7 and true_color == 3:
                raise VideoFault("reserved OSD true-color mode")
            bpp = (1, 2, 4, 8, 16, 16, 16, 32)[fmt]
            pitch = (stride * bpp + 7) // 8
            dma_bytes += pitch * rh
            if dma_bytes > 32 * 1024 * 1024:
                raise VideoFault("OSD DMA exceeds the 32 MiB capture limit")
            data = ram_bytes(bus, pixels, pitch * rh)
            digest.update(data)
            palette = b""
            if fmt < 4:
                palette = ram_bytes(bus, palette_address, (1 << bpp) * 4)
                digest.update(palette)
            regions.append((control, alpha, left + x_offset, top + y_offset, rw, rh, fmt, true_color, bpp, pitch, data, palette))
            if infer_width:
                width = max(width, left + x_offset + rw)
            if infer_height:
                height = max(height, top + y_offset + rh)
            if alpha & 0x80000000:
                break
            head = next_head
            if not head or head & 3:
                raise VideoFault("invalid next OSD descriptor")
        else:
            raise VideoFault("OSD list exceeds 16 regions")
        if not (0 < width <= 1920 and 0 < height <= 1080):
            raise VideoFault("OSD view exceeds supported 1920×1080 bounds")
        key = digest.hexdigest()
        if self.key == key:
            return self.cached
        canvas = bytearray(width * height * 4)
        for region in regions:
            control, alpha, left, top, rw, rh, fmt, true_color, bpp, pitch, data, palette = region
            argb = bool(control & (1 << 26))
            weight = ((control >> 16) & 127) * 255 // 127 if control & (1 << 23) else 255
            if alpha & (1 << 24):
                weight = weight * ((alpha >> 16) & 255) // 255
            for y in range(max(0, -top), min(rh, height - top)):
                for x in range(max(0, -left), min(rw, width - left)):
                    offset = y * pitch + x * bpp // 8
                    if fmt < 4:
                        index = (data[offset] >> (8 - bpp - ((x * bpp) & 7))) & ((1 << bpp) - 1)
                        value = int.from_bytes(palette[index * 4:index * 4 + 4], "little")
                        red, green, blue, opacity = (value >> 16) & 255, (value >> 8) & 255, value & 255, value >> 24
                    else:
                        value = int.from_bytes(data[offset:offset + bpp // 8], "little")
                        if fmt == 4:
                            if argb:
                                red, green, blue, opacity = expand((value >> 8) & 15, 4), expand((value >> 4) & 15, 4), expand(value & 15, 4), expand(value >> 12, 4)
                            else:
                                red, green, blue, opacity = expand(value >> 12, 4), expand((value >> 8) & 15, 4), expand((value >> 4) & 15, 4), expand(value & 15, 4)
                        elif fmt == 5:
                            if argb:
                                red, green, blue, opacity = expand((value >> 10) & 31, 5), expand((value >> 5) & 31, 5), expand(value & 31, 5), (value >> 15) * 255
                            else:
                                red, green, blue, opacity = expand(value >> 11, 5), expand((value >> 6) & 31, 5), expand((value >> 1) & 31, 5), (value & 1) * 255
                        elif fmt == 6:
                            red, green, blue, opacity = expand(value >> 11, 5), expand((value >> 5) & 63, 6), expand(value & 31, 5), 255
                        elif true_color in (1, 2):
                            red, green, blue, opacity = (value >> 16) & 255, (value >> 8) & 255, value & 255, 255
                            if true_color == 2:
                                red, blue = blue, red
                        elif argb:
                            red, green, blue, opacity = (value >> 16) & 255, (value >> 8) & 255, value & 255, value >> 24
                        else:
                            red, green, blue, opacity = value >> 24, (value >> 16) & 255, (value >> 8) & 255, value & 255
                    if control & (1 << 15):
                        raise VideoFault("OSD color key is not implemented")
                    opacity = opacity * weight // 255
                    destination = ((top + y) * width + left + x) * 4
                    if opacity == 255:
                        canvas[destination:destination + 4] = bytes((red, green, blue, 255))
                    elif opacity:
                        previous = canvas[destination + 3]
                        output_alpha = opacity + previous * (255 - opacity) // 255
                        for channel, color in enumerate((red, green, blue)):
                            canvas[destination + channel] = (color * opacity + canvas[destination + channel] * previous * (255 - opacity) // 255) // output_alpha
                        canvas[destination + 3] = output_alpha
        if scaled:
            source_width = width
            offsets = [(x * hzoom >> 12) * 4 for x in range(output_width)]
            rows = {}
            output = bytearray()
            for y in range(output_height):
                source_y = y * vzoom >> 12
                if source_y not in rows:
                    row = memoryview(canvas)[source_y * source_width * 4:(source_y + 1) * source_width * 4]
                    rows[source_y] = b''.join(row[x:x + 4] for x in offsets)
                output.extend(rows[source_y])
            canvas, width, height = output, output_width, output_height
        result = {"status": "ready", "width": width, "height": height, "regions": len(regions),
                  "id": key, "source": "guest OSD descriptors and RAM", "png": png_rgba(width, height, canvas)}
        if scaled:
            result['scaling'] = 'nearest-neighbor approximation; hardware filter phases are not modeled'
        self.key, self.cached = key, result
        return result
