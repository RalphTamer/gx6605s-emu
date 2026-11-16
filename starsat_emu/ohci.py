"""OHCI register-level startup with disconnected root ports; no transfer DMA.

Initial data is from this receiver. Functional reset, interrupt and port
commands follow OHCI definitions; hardware delays and CPU IRQ delivery are
absent. The frame register advances, but HCCA DMA is not implemented.
"""
from .memory import EmulationStop


class OHCI:
    size = 0x1000

    def __init__(self, base, clock, instruction_hz=100000):
        self.base, self.clock, self.resets = base, clock, 0
        self.instruction_hz = instruction_hz
        self.registers = {offset: 0 for offset in range(0, 0x58, 4)}
        self.registers.update({0: 0x110, 0x34: 0x2edf, 0x44: 0x628, 0x48: 0x02000901})
        self.last_tick, self.fraction = clock(), 0

    def _sync(self):
        now = self.clock()
        elapsed = max(0, now - self.last_tick)
        self.last_tick = now
        if self.registers[4] & 0xc0 == 0x80:
            frames, self.fraction = divmod(self.fraction + elapsed * 1000, self.instruction_hz)
            if frames:
                total = self.registers[0x3c] + frames
                self.registers[0xc] |= 4 | (0x20 if total > 0xffff else 0)
                self.registers[0x3c] = total & 0xffff
            remaining_toggle = (self.registers[0x34] if frames else self.registers[0x38]) & 0x80000000
            self.registers[0x38] = remaining_toggle | max(0, (self.registers[0x34] & 0x3fff) - self.fraction * 12000 // self.instruction_hz)

    def access(self, offset, size, value=None):
        if size not in (1, 2, 4) or offset % size or (offset & 3) + size > 4:
            raise EmulationStop('unsupported-device-access', 'OHCI requires naturally aligned register access')
        word, shift = offset & ~3, (offset & 3) * 8
        if word not in self.registers:
            raise EmulationStop('unsupported-device-access', 'OHCI register offset=0x%x is not modeled' % offset)
        self._sync()
        mask = ((1 << (8 * size)) - 1) << shift
        if value is None:
            result = self.registers[0x10] if word == 0x14 else self.registers[word]
            return (result & mask) >> shift
        bits = (value << shift) & mask
        merged = (self.registers[word] & ~mask) | bits
        if word == 4:
            if merged & ~0x7ff:
                raise EmulationStop('unsupported-device-access', 'unknown OHCI control mode')
            self.registers[word] = (merged & ~0x200) | (self.registers[word] & 0x200)
        elif word == 8:
            if bits & ~0xf:
                raise EmulationStop('unsupported-device-access', 'unknown OHCI command')
            if bits & 6:
                raise EmulationStop('unimplemented-usb-transfer', 'OHCI transfer lists and DMA are not implemented')
            if bits & 1:
                # Host-controller software reset enters USB suspend. Root-hub
                # port state and descriptors survive this reset.
                routing = self.registers[4] & 0x100
                for reg in range(4, 0x44, 4):
                    self.registers[reg] = 0
                self.registers[4] = routing | 0xc0
                self.registers[0x34] = 0x2edf
                self.registers[0x44] = 0x628
                self.last_tick, self.fraction = self.clock(), 0
                self.resets += 1
            if bits & 8:
                self.registers[4] &= ~0x100
                self.registers[0xc] |= 1 << 30
        elif word == 0xc:
            self.registers[word] &= ~(bits & 0x4000007f)
        elif word in (0x10, 0x14):
            enabled = self.registers[0x10]
            self.registers[0x10] = (enabled | bits if word == 0x10 else enabled & ~bits) & 0xc000007f
        elif word in (0x18, 0x20, 0x28):
            self.registers[word] = merged & (0xffffff00 if word == 0x18 else 0xfffffff0)
        elif word in (0x34, 0x40, 0x44):
            self.registers[word] = merged & {0x34: 0xffff3fff, 0x40: 0x3fff, 0x44: 0xfff}[word]
        elif word == 0x48:
            # NDP=1 and device-type/reserved fields are not writable.
            writable = 0xff001b00
            self.registers[word] = (self.registers[word] & ~writable) | (merged & writable)
        elif word == 0x4c:
            self.registers[word] = merged & 0x00020002  # sole downstream port
        elif word == 0x50:
            status = self.registers[word]
            if bits & 0x8000:
                status |= 0x8000
            if bits & 0x80000000:
                status &= ~0x8000
            if bits & 1:
                self.registers[0x54] &= ~0x100
            if bits & 0x10000:
                self.registers[0x54] |= 0x100
            self.registers[word] = status & ~(bits & 0x20000)
        elif word == 0x54:
            # Disconnected virtual port: no enable/reset/suspend completion
            # or connect/change event can be invented by a software write.
            status = self.registers[word] & ~(bits & 0x1f0000)
            if bits & 0x100:
                status |= 0x100
            if bits & 0x200:
                status &= ~0x100
            self.registers[word] = status
        else:
            raise EmulationStop('readonly-write', 'OHCI read-only register offset=0x%x' % word)

    def snapshot(self):
        self._sync()
        enabled = self.registers[0x10]
        return {'base': hex(self.base), 'registers': {hex(k): hex(v) for k, v in self.registers.items()},
                'reset_count': self.resets, 'irq_pending': bool(enabled & 0x80000000 and enabled & self.registers[0xc] & 0x4000007f),
                'attached_devices': [], 'hcca_and_transfer_dma': False, 'cpu_interrupt_delivery': False}


class OHCIControllers:
    base, size = 0x00a00000, 0x101000

    def __init__(self, clock, instruction_hz=100000):
        self.controllers = [OHCI(base, clock, instruction_hz) for base in (0x00a00000, 0x00b00000)]

    def contains(self, address, size):
        return any(c.base <= address and address + size <= c.base + c.size for c in self.controllers)

    def access(self, offset, size, value=None):
        address = self.base + offset
        for controller in self.controllers:
            if controller.base <= address and address + size <= controller.base + controller.size:
                return controller.access(address - controller.base, size, value)
        raise EmulationStop('unmapped-access', 'outside either OHCI controller window')

    def snapshot(self):
        return [c.snapshot() for c in self.controllers]
