"""Functional EHCI register model with two disconnected root ports.

Capability words and the initial halted state were read from this receiver.
Register definitions/reset/start handshakes follow the EHCI interface (see
docs/references.md). No USB device, transfer DMA, or CPU IRQ delivery exists.
Reset and schedule transitions are immediate functional approximations.
"""
from .memory import EmulationStop


class EHCI:
    base = 0x00900000
    size = 0x1000
    capabilities = {0: 0x01000010, 4: 0x00002112, 8: 0x0000a022}
    operational = 0x10

    def __init__(self, clock, instruction_hz=100000):
        self.clock = clock
        self.instruction_hz = instruction_hz
        self.reset_count = 0
        self._reset()

    def _reset(self):
        self.registers = {0x10: 0x00080000, 0x14: 0x1000, 0x18: 0,
                          0x1c: 0, 0x20: 0, 0x24: 0, 0x28: 0, 0x50: 0,
                          0x54: 0x2000, 0x58: 0x2000}
        self.last_tick = self.clock()
        self.fraction = 0

    def _sync(self):
        now = self.clock()
        elapsed = max(0, now - self.last_tick)
        self.last_tick = now
        if self.registers[0x10] & 1:
            # Same virtual clock as RTC/timers: 8,000 microframes per second.
            microframes, self.fraction = divmod(self.fraction + elapsed * 8000, self.instruction_hz)
            index = self.registers[0x1c] + microframes
            if index >= 0x4000:
                self.registers[0x14] |= 8
            self.registers[0x1c] = index & 0x3fff

    def access(self, offset, size, value=None):
        if size not in (1, 2, 4) or offset % size or (offset & 3) + size > 4:
            raise EmulationStop('unsupported-device-access', 'EHCI access requires a naturally aligned byte, halfword or word')
        word, shift = offset & ~3, (offset & 3) * 8
        mask = ((1 << (8 * size)) - 1) << shift
        self._sync()
        if word in self.capabilities:
            if value is not None:
                raise EmulationStop('readonly-write', 'EHCI capability registers are read-only')
            return (self.capabilities[word] & mask) >> shift
        if word not in self.registers:
            raise EmulationStop('unsupported-device-access', 'EHCI register offset=0x%x is not modeled' % offset)
        if value is None:
            return (self.registers[word] & mask) >> shift
        bits = (value << shift) & mask
        merged = (self.registers[word] & ~mask) | bits
        if word == 0x10:
            # EHCI 1.0 command bits implemented by this controller/model.
            if merged & ~0x00ff0b7f or ((merged >> 2) & 3) == 3:
                raise EmulationStop('unsupported-device-access', 'unsupported EHCI command mode')
            if merged & 2:
                self.reset_count += 1
                self._reset()
                return
            self.registers[word] = merged & ~0x40
            status = self.registers[0x14] & ~(0x1000 | 0xc000)
            if merged & 1:
                status |= (merged & 0x30) << 10
            else:
                status |= 0x1000
            if bits & 0x40:
                # Empty virtual asynchronous schedule: no pending USB transfer.
                status |= 0x20
            self.registers[0x14] = status
        elif word == 0x14:
            self.registers[word] &= ~(bits & 0x3f)  # W1C; do not clear adjacent bytes
        elif word == 0x18:
            self.registers[word] = merged & 0x3f
        elif word == 0x1c:
            self.registers[word] = merged & 0x3fff
            self.fraction = 0
        elif word == 0x20:
            if merged:
                raise EmulationStop('unsupported-device-access', 'receiver EHCI does not advertise 64-bit DMA')
        elif word == 0x24:
            self.registers[word] = merged & 0xfffff000
        elif word == 0x28:
            self.registers[word] = merged & 0xffffffe0
        elif word == 0x50:
            previous = self.registers[word]
            self.registers[word] = merged & 1
            if previous != self.registers[word]:
                for port in (0x54, 0x58):
                    if merged & 1:
                        self.registers[port] &= ~0x2000
                    else:
                        self.registers[port] |= 0x2000
        else:
            if merged & 0xf0000:
                raise EmulationStop('unsupported-device-access', 'USB port test modes are not implemented')
            # No attached device: connect/enable/line/change bits remain clear.
            # Port reset is software-controlled (unlike controller reset).
            self.registers[word] = merged & (0x700000 | 0x2000 | 0x1000 | 0x100)

    def snapshot(self):
        self._sync()
        return {'capabilities': {hex(k): hex(v) for k, v in self.capabilities.items()},
                'registers': {hex(k): hex(v) for k, v in self.registers.items()},
                'reset_count': self.reset_count,
                'irq_pending': bool(self.registers[0x14] & self.registers[0x18] & 0x3f),
                'attached_devices': [], 'dma_transfers_implemented': False,
                'cpu_interrupt_delivery': False}
