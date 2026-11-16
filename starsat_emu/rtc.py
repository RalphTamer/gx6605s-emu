"""GX6605S RTC counter with deterministic instruction-based virtual time.

Register layout follows gx6605s-rtc.h; US is in units of ten microseconds.
The receiver kernels convert seconds/minutes/hours from packed BCD, while
using the millisecond field directly. Those counter encodings are distinct.
Calendar/alarm values are stored, but calendar rollover and IRQs are absent.
"""
from .memory import EmulationStop


class RTC:
    base = 0x00209000
    size = 0x88
    offsets = {0, 4, 12, 16, *range(0x14, 0x38, 4), *range(0x3c, 0x60, 4), *range(0x64, 0x88, 4)}
    counter = {0x64: (10, 100), 0x68: (1000, 1000), 0x6c: (1000000, 60),
               0x70: (60000000, 60), 0x74: (3600000000, 24)}
    bcd = {0x6c, 0x70, 0x74}

    def __init__(self, clock, instruction_hz=100000):
        self.clock = clock
        self.instruction_hz = instruction_hz
        self.registers = {o: 0 for o in self.offsets}
        self.start_tick = clock()
        self.initial_us = 0

    def elapsed_us(self):
        return self.initial_us + ((self.clock() - self.start_tick) * 1000000 // self.instruction_hz if self.registers[0] & 1 else 0)

    def access(self, offset, size, value=None):
        if size != 4 or offset not in self.offsets:
            raise EmulationStop("unsupported-device-access", f"RTC offset=0x{offset:x} size={size}")
        if value is None:
            if offset in self.counter:
                unit, modulo = self.counter[offset]
                count = (self.elapsed_us() // unit) % modulo
                return ((count // 10) << 4) | (count % 10) if offset in self.bcd else count
            return self.registers[offset]
        value &= 0xffffffff
        encoded_value = value
        if offset == 0:
            if value & 12:
                raise EmulationStop("unsupported-device-access", "RTC alarm delivery is not implemented")
            self.initial_us = self.elapsed_us()
            self.start_tick = self.clock()
        elif offset in self.counter:
            unit, modulo = self.counter[offset]
            if offset in self.bcd:
                if value & ~0xff or (value & 15) > 9 or value >> 4 > 9:
                    raise EmulationStop('unsupported-device-access', 'RTC time requires packed BCD')
                value = (value >> 4) * 10 + (value & 15)
                if value >= modulo:
                    raise EmulationStop('unsupported-device-access', 'RTC time field is out of range')
            current = self.elapsed_us()
            self.initial_us = current + ((value % modulo) - ((current // unit) % modulo)) * unit
            self.start_tick = self.clock()
        self.registers[offset] = encoded_value
