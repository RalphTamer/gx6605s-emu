"""GX6605S GA command DMA, decoded from the local receiver kernel.

Only the observed unscaled 32-bit source-copy command is supported. Other
operations stop, without reporting completion. This model is deliberately
separate from the OSD renderer: all output must be written to actual guest RAM.
"""
from collections import deque
import struct
import weakref
from .memory import EmulationStop


class GraphicsAccelerator:
    base = 0x04700000
    size = 0x2c
    irq = 62
    offsets = (4, 8, 0x10, 0x14, 0x18, 0x1c, 0x24, 0x28)

    def __init__(self, bus, reset=None):
        self.bus_ref = weakref.ref(bus)
        # Cold RAM-probe captures establish these three initial values. The
        # command/configuration words remain unknown until the guest writes.
        self.registers = {4: None, 8: None, 0x10: 0, 0x14: None, 0x18: 0,
                          0x1c: 0, 0x24: None, 0x28: None}
        if reset is not None:self.registers.update(reset)
        self.pending = False
        self.lists = self.commands = self.bytes_written = 0
        self.history = deque(maxlen=32)

    def contains(self, address, size):
        return size == 4 and address - self.base in self.offsets

    def ram(self, address, length, write=False):
        # Restrict all DMA to the receiver's 64 MiB DRAM. No MMIO access, host
        # files, wraparound, or BootROM access can result from a job descriptor.
        if not (0x10000000 <= address and 0 < length <= 32 * 1024 * 1024 and address + length <= 0x14000000):
            raise EmulationStop('invalid-graphics-dma', 'GA buffer is outside bounded DRAM')
        for region in self.bus_ref().regions:
            offset = address - region.base
            if 0 <= offset and offset + length <= len(region.data) and (not write or not region.readonly):
                return region.data, offset
        raise EmulationStop('invalid-graphics-dma', 'GA buffer has no accessible DRAM mapping')

    def decode(self, raw):
        w = struct.unpack('<20I', raw)
        if w[0] != 0x90400707 or w[1] != 0 or w[18] & 15 != 1 or w[19] & 0x3ffff:
            raise EmulationStop('unsupported-graphics-command', 'GA mode/control is not implemented: ' + ','.join(hex(v) for v in (w[0],w[1],w[18]&15,w[19]&0x3ffff)))
        if (w[2:5] != (0xbbc29800, 0xbbc29807, 0xf2e60a00) or
                any(w[5:12]) or w[15] & 15 or w[16] & 15 or w[17] & 15):
            raise EmulationStop('unsupported-graphics-command', 'GA format/extra fields are not implemented')
        src_pitch, src_width = w[15] >> 18, (w[15] >> 4) & 0x3fff
        src_height = w[16] >> 18
        bg_pitch, bg_width, bg_height = (w[16] >> 4) & 0x3fff, w[17] >> 18, (w[17] >> 4) & 0x3fff
        dst_pitch, width, height = w[18] >> 18, (w[18] >> 4) & 0x3fff, w[19] >> 18
        if (not 0 < width <= 4096 or not 0 < height <= 4096 or
                (src_width, src_height) != (width, height) or (bg_width, bg_height) != (width, height) or
                not width <= src_pitch <= 8192 or not width <= dst_pitch <= 8192 or not width <= bg_pitch <= 8192):
            raise EmulationStop('unsupported-graphics-command', 'GA scaling or geometry is not implemented')
        source, source_offset = self.ram(w[12], ((height - 1) * src_pitch + width) * 4)
        target, target_offset = self.ram(w[14], ((height - 1) * dst_pitch + width) * 4, True)
        self.ram(w[13], ((height - 1) * bg_pitch + width) * 4)
        return {'source': w[12], 'target': w[14], 'width': width, 'height': height,
                'src_pitch': src_pitch, 'dst_pitch': dst_pitch, 'words': [hex(v) for v in w]}, (source, source_offset, target, target_offset)

    def execute(self, launch):
        count = launch & 65535
        if not self.registers[0x10] & 1 or not 0 < count <= 1024 or self.registers[8] != count * 80 or self.registers[4] is None:
            raise EmulationStop('unsupported-graphics-list', 'GA list requires enabled engine and count × 80 bytes')
        data, offset = self.ram(self.registers[4], count * 80)
        raw = bytes(data[offset:offset + count * 80])
        jobs = [self.decode(raw[i*80:(i+1)*80]) for i in range(count)]
        for job, (source, src_offset, target, dst_offset) in jobs:
            width, height = job['width'], job['height']
            # Snapshot source rows to make overlap deterministic. Hardware
            # direction flags for overlapping rectangles are not implemented.
            rows = [bytes(source[src_offset + y*job['src_pitch']*4:src_offset + y*job['src_pitch']*4 + width*4]) for y in range(height)]
            for y, row in enumerate(rows):
                start = dst_offset + y*job['dst_pitch']*4
                target[start:start+len(row)] = row
            self.commands += 1
            self.bytes_written += width*height*4
            self.bus_ref().activity += 1
            self.history.append(job)
        self.lists += 1
        self.pending = True

    def irq_pending(self):
        return self.pending and bool(self.registers[0x1c] & 1)

    def access(self, offset, size, value=None):
        if size != 4 or offset not in self.offsets:
            raise EmulationStop('unsupported-device-access', 'GA requires a modeled word register')
        if value is None:
            if self.registers[offset] is None:
                raise EmulationStop('unknown-device-reset', 'GA configuration has not been initialized')
            return self.registers[offset] | (0x10 if offset == 0x1c and self.pending else 0)
        value &= 0xffffffff
        if offset == 0x18 and value & 0x10000 and not self.registers[0x18] & 0x10000:
            self.execute(value)
        if offset == 0x1c:
            if value & 2:self.pending = False
            value &= ~0x10  # measured completion bit is read-only, ACK bit 1 persists
        self.registers[offset] = value

    def snapshot(self):
        return {'status': 'unscaled-rgba-copy-only', 'lists': self.lists,
                'commands': self.commands, 'bytes_written': self.bytes_written,
                'irq': self.irq, 'irq_pending': self.irq_pending(),
                'recent_commands': list(self.history),
                'limitations': 'RGBA copy/stride/alpha preservation and completion bit hardware-verified; IRQ routing inferred from native driver; filters, blending, fills, overlap direction and other formats absent'}
