# SPDX-License-Identifier: MIT
# Register layout reference: Copyright (c) 2021 Sanpe <sanpeqf@gmail.com>.
"""GX6605S GPIO bank: direction, pin levels, output set/clear and inversion.

Layout follows the MIT-licensed xboot GX6605S port. External pin levels belong
to the machine profile; this model does not infer them from the firmware.
"""
from .memory import EmulationStop


class GPIO:
    base = 0x00305000
    size = 0x24

    def __init__(self, inputs):
        self.inputs = inputs & 0xffffffff
        self.direction = self.output = 0
        self.config = {0x10: 0, 0x18: 0, 0x1c: 0, 0x20: 0}

    def pins(self):
        external = self.inputs ^ self.config[0x1c]
        return ((self.output & self.direction) | (external & ~self.direction)) & 0xffffffff

    def access(self, offset, size, value=None):
        if size != 4 or offset % 4 or not 0 <= offset < self.size:
            raise EmulationStop("unsupported-device-access", f"GPIO offset=0x{offset:x} size={size}")
        if value is None:
            if offset == 0:
                return self.direction
            if offset in (4, 8, 12):
                return self.pins()
            if offset == 0x14:
                return 0  # no external transitions/interrupt sources
            return self.config[offset]
        value &= 0xffffffff
        if offset == 0:
            self.direction = value
        elif offset == 4:
            self.output = value
        elif offset == 8:
            self.output |= value & self.direction
        elif offset == 12:
            self.output &= ~(value & self.direction)
        elif offset != 0x14:
            self.config[offset] = value

    def snapshot(self):
        return {"inputs": hex(self.inputs), "direction": hex(self.direction),
                "output": hex(self.output), "pins": hex(self.pins())}
