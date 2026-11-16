# SPDX-License-Identifier: LGPL-2.0-or-later
# Copyright (c) 2024 Alibaba Group. All rights reserved.
# Adapted semantics from Alibaba C-SKY QEMU mmu.c. See docs/references.md.
"""ABIv1 software-managed TLB and supervisor direct segments.

Hardware page-table refill and exception delivery are intentionally absent:
a missing translation is a reported fault, never an identity-map fallback.
"""
from dataclasses import dataclass
from .memory import EmulationStop


@dataclass
class TLBEntry:
    virtual: int
    asid: int
    mask: int
    bits: int
    low0: int
    low1: int

    @property
    def global_entry(self):
        return bool(self.low0 & self.low1 & 1)


class MMU:
    def __init__(self):
        self.registers = [0] * 32
        # CPU reset values used by the C-SKY QEMU reference.
        self.registers[30], self.registers[31] = 0x1e, 0x16
        self.entries = {}
        self.round_robin = [0] * 64
        self.last_command = 0
        self.cache = {}

    def page_bits(self):
        mask = (self.registers[6] >> 13) & 0xfff
        sizes = {0: 12, 3: 14, 15: 16, 63: 18, 255: 20, 1023: 22, 4095: 24}
        if mask not in sizes:
            raise EmulationStop("unsupported-page-size", f"MPR=0x{self.registers[6]:08x}")
        return sizes[mask]

    def register(self, number, value=None):
        if number not in (*range(9), 30, 31):
            raise EmulationStop("unsupported-mmu-register", f"cp15 cr{number}")
        if value is None:
            return self.registers[number]
        value &= 0xffffffff
        self.cache.clear()
        if number == 8:
            self.command(value)
        else:
            self.registers[number] = value

    def command(self, value):
        self.last_command = value
        self.cache.clear()
        r = self.registers
        if value & (1 << 27):
            self.entries = {i: entry for i, entry in self.entries.items() if entry.asid != value & 255}
        elif value & (1 << 25):
            self.entries.pop(r[0] & 127, None)
        elif value & (1 << 31):
            virtual = r[4] & ~(r[6] | 0x1fff)
            found = [i for i, e in self.entries.items() if e.virtual == virtual and (e.global_entry or e.asid == r[4] & 255)]
            r[0] = min(found) if found else r[0] | 0x80000000
        elif value & ((1 << 29) | (1 << 28)):
            bits = self.page_bits()
            if value & (1 << 29):
                index = r[0] & 127
            else:
                index = (r[4] >> (bits + 1)) & 63
                bank = self.round_robin[index]
                self.round_robin[index] ^= 1
                index += bank * 64
            self.entries[index] = TLBEntry(r[4] & ~(r[6] | 0x1fff), r[4] & 255, r[6], bits, r[2], r[3])
        elif value & (1 << 30):
            entry = self.entries.get(r[0] & 127)
            if entry is None:
                r[2] = r[3] = r[4] = r[6] = 0
            else:
                r[2], r[3], r[4], r[6] = entry.low0, entry.low1, entry.virtual | entry.asid, entry.mask
        elif value & (1 << 26):
            self.entries.clear()
        elif value:
            raise EmulationStop("unsupported-mmu-command", f"MCIR=0x{value:08x}")

    def translate(self, address, write=False, supervisor=True):
        address &= 0xffffffff
        key = (address >> 12, write, supervisor, self.registers[4] & 255)
        if key in self.cache:
            return self.cache[key] | (address & 4095)
        physical = self._translate(address, write, supervisor)
        self.cache[key] = physical & ~4095
        return physical

    def _translate(self, address, write, supervisor):
        if 0x80000000 <= address < 0xc0000000:
            if not supervisor:
                raise EmulationStop("mmu-privilege-fault", f"virtual=0x{address:08x}")
            segment = 30 if address < 0xa0000000 else 31
            value = self.registers[segment]
            if not value & 2 or (write and not value & 4):
                raise EmulationStop("mmu-permission-fault", f"virtual=0x{address:08x} segment=MSA{segment - 30}")
            return (address & 0x1fffffff) + (value & 0xe0000000)
        if address >= 0xc0000000 and not supervisor:
            raise EmulationStop("mmu-privilege-fault", f"virtual=0x{address:08x}")
        asid = self.registers[4] & 255
        for entry in self.entries.values():
            if address & ~(entry.mask | 0x1fff) != entry.virtual or not (entry.global_entry or entry.asid == asid):
                continue
            low = entry.low1 if (address >> entry.bits) & 1 else entry.low0
            if not low & 2 or (write and not low & 4):
                raise EmulationStop("mmu-permission-fault", f"virtual=0x{address:08x} write={write}")
            page_mask = (1 << entry.bits) - 1
            return ((low << 6) & ~page_mask & 0xffffffff) | (address & page_mask)
        raise EmulationStop("mmu-miss", f"virtual=0x{address:08x} asid={asid}; hardware refill is not implemented")

    def snapshot(self):
        return {"registers": {str(i): hex(v) for i, v in enumerate(self.registers) if v},
                "last_command": hex(self.last_command),
                "tlb": [{"index": i, **vars(e)} for i, e in sorted(self.entries.items())]}
