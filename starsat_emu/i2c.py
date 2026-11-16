"""GX6605S polling I2C controller on an explicitly empty virtual bus.

Register layout and DONE/WACK polarity follow xboot's GX6605S driver. Every
address/data transfer receives NACK because no slave is attached. STOP can
complete independently. Timing, interrupts, tuners and EEPROMs are unmodeled.
"""
from collections import deque
from .memory import EmulationStop


class I2C:
    base = 0x00205000
    size = 0x0c
    INTEN, ENABLE, START, STOP, DONE, RACK, WACK = (1 << bit for bit in (7, 6, 5, 4, 3, 2, 1))

    def __init__(self):
        self.control = self.data = self.clock = 0
        self.active = False
        self.transfers = 0
        self.transactions = deque(maxlen=32)

    def access(self, offset, size, value=None):
        if size != 4 or offset not in (0, 4, 8):
            raise EmulationStop("unsupported-device-access", f"I2C offset=0x{offset:x} size={size}")
        if value is None:
            return {0: self.control, 4: self.data, 8: self.clock}[offset]
        value &= 0xffffffff
        if offset == 8:
            self.clock = value
        elif offset == 4:
            self.data = value & 255
        else:
            if value & ~0xfe:
                raise EmulationStop("unsupported-device-access", f"unmodeled I2C control bits 0x{value:x}")
            self.control = value & (self.INTEN | self.ENABLE | self.RACK)
            if not value & self.ENABLE:
                self.active = False
                return
            self.transfers += 1
            self.control |= self.DONE
            if value & self.STOP:
                self.active = False
                self.transactions.append({"operation": "stop"})
            else:
                self.control |= self.WACK  # NACK; no slave ever acknowledges
                if value & self.START:
                    self.active = True
                    self.transactions.append({"operation": "address", "address": self.data >> 1,
                                              "read": bool(self.data & 1), "ack": False})
                else:
                    self.transactions.append({"operation": "data", "ack": False})
                    if value & self.RACK:
                        self.data = 255  # undriven SDA, not a slave response

    def snapshot(self):
        return {"control": hex(self.control), "data": hex(self.data), "clock": hex(self.clock),
                "slaves": [], "active": self.active, "transfers": self.transfers,
                "irq_pending": bool(self.control & self.INTEN and self.control & self.DONE),
                "interrupt_delivery": "unimplemented", "recent_transactions": list(self.transactions)}


class I2CControllers:
    """Three independently configured instances observed in the eCos device table."""
    bases = (0x00203000, 0x00205000, 0x00206000)
    base = bases[0]
    size = bases[-1] - base + 0x0c

    def __init__(self):
        self.controllers = [I2C() for _ in self.bases]

    def contains(self, address, size):
        return any(base <= address and address + size <= base + 12 for base in self.bases)

    def access(self, offset, size, value=None):
        address = self.base + offset
        index = next((i for i, base in enumerate(self.bases) if base <= address < base + 12), None)
        if index is None:
            raise EmulationStop("unsupported-device-access", f"I2C instance offset=0x{offset:x}")
        return self.controllers[index].access(address - self.bases[index], size, value)

    def snapshot(self):
        return {"controllers": [{"base": hex(self.bases[i]), **c.snapshot()}
                                for i, c in enumerate(self.controllers)]}
