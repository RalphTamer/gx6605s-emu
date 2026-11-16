"""Read-only OTP controller inferred from the supplied bootloader and kernel.

An explicit capture can supply all 2048 byte addresses. Without a capture,
the historical exploratory profile uses synthetic zero bytes. Neither mode
permits programming or manufactures an authentication result.
"""
from .memory import EmulationStop


class OTP:
    base = 0x00f80000
    size = 16

    def __init__(self, info=None, data=None):
        if data is not None and (not isinstance(data, bytes) or len(data) != 2048):
            raise ValueError('OTP capture requires exactly 2048 bytes')
        self.data = bytes(2048) if data is None else data
        self.captured = data is not None
        # V3.93 reads this latch and sets bit 0 before its read loop. The
        # physical RAM bootstrap also writes 1 here. Initial enabled state
        # is a bootstrap assumption, not a measured native cold reset.
        self.configuration = 1
        self.control = 0
        self.result = 0
        self.complete = False
        self.reads = 0
        self.info = info

    def access(self, offset, size, value=None):
        if size != 4 or offset not in (0, 4, 8, 12):
            raise EmulationStop("unsupported-device-access", f"OTP offset=0x{offset:x} size={size}")
        if offset == 4:
            if value is None:
                return self.configuration
            if value not in (0, 1):
                raise EmulationStop('unsupported-device-access', 'unknown OTP configuration bits')
            self.configuration = value
            return None
        if value is None:
            if offset == 12:
                if self.info is None:
                    raise EmulationStop("unknown-device-reset", "OTP auxiliary information register is not configured")
                return self.info
            return self.control if offset == 0 else 0x400 | (0x200 if self.complete else 0) | self.result
        if offset != 0 or value & ~0x7ff8:
            raise EmulationStop("unsupported-device-access", "OTP writes/programming are not implemented")
        if value & 0x4000 and not self.control & 0x4000:
            address = (value >> 3) & 0x7ff
            self.result = self.data[address]
            self.complete = True
            self.reads += 1
        # Deasserting READ at byte address zero also writes zero. Preserve the
        # result through that falling edge, as the native driver polls DONE
        # after clearing READ. A subsequent zero ends the transaction.
        if value == 0 and not self.control & 0x4000:
            self.complete = False
        self.control = value
