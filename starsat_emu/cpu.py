# SPDX-License-Identifier: LGPL-2.0-or-later
# Copyright (c) 2024 Alibaba Group. All rights reserved.
# Instruction encoding/semantics reference: Alibaba's C-SKY QEMU translator.
# See docs/references.md for attribution and exact revision.
"""Small C-SKY ABIv1 interpreter; incomplete, deterministic, no host execution."""
from dataclasses import dataclass
from functools import lru_cache
from .memory import EmulationStop
from .mmu import MMU

MASK = 0xffffffff


def signed(value, bits=32):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


@dataclass(frozen=True)
class Instruction:
    op: str
    args: tuple = ()

    def __str__(self):
        op, a = self.op, self.args
        if op in ("load", "store"):
            z, x, offset, size = a
            name = ("st" if op == "store" else "ld") + {1: ".b", 2: ".h", 4: ".w"}[size]
            return f"{name} r{z}, (r{x}, {offset})"
        if op == "lrw":
            return f"lrw r{a[0]}, [0x{a[1]:08x}]"
        if op in ("jmpi", "jsri"):
            return f"{op} [0x{a[0]:08x}]"
        if op in ("br", "bsr", "bt", "bf"):
            return f"{op} 0x{a[0]:08x}"
        if op in ("mfcr", "mtcr", "cpwcr", "cprcr"):
            return f"{op} r{a[0]}, cr{a[1]}"
        if op in ("cpseti", "psrset", "psrclr", "unsupported"):
            return op + " " + ", ".join(str(v) for v in a)
        registers = {"mov", "movt", "movf", "mult", "subu", "rsub", "ixw", "and", "xor",
                     "addu", "ixh", "or", "andn", "cmphs", "cmplt", "tst", "cmpne", "lsl", "lsr", "asr",
                     "bgenr", "addc", "subc", "mulsh"}
        if op in registers:
            return f"{op} r{a[0]}, r{a[1]}"
        return op + (" r" + str(a[0]) + (", " + str(a[1]) if len(a) > 1 else "") if a else "")


@lru_cache(maxsize=131072)
def decode(word, pc):
    """Decode implemented ABIv1 encodings; other encodings remain explicit."""
    high, sub, y, x = word >> 12, (word >> 8) & 15, (word >> 4) & 15, word & 15
    imm = (word >> 4) & 31
    if word in (0, 1, 2, 3, 4, 5, 6, 7):
        return Instruction(("bkpt", "sync", "rte", "rfi", "stop", "wait", "doze", "idly4")[word])
    if word >> 4 == 1:
        return Instruction("cpseti", (x,))
    if word >> 4 == 0x11f:
        return Instruction("psrset" if x & 8 else "psrclr", (x & 7,))
    if high == 0:
        if sub == 0:
            op = {2: "mvc", 3: "mvcv", 4: "ldq", 5: "stq", 6: "ldm", 7: "stm",
                  8: "dect", 9: "decf", 10: "inct", 11: "incf", 12: "jmp", 13: "jsr",
                  14: "ff1", 15: "brev"}.get(y)
            if op:
                return Instruction(op, (x,))
        if sub == 1:
            op = ("xtrb3", "xtrb2", "xtrb1", "xtrb0", "zextb", "sextb", "zexth", "sexth",
                  "declt", "tstnbz", "decgt", "decne", "clrt", "clrf", "abs", "not")[y]
            return Instruction(op, (x,))
        op = {2: "movt", 3: "mult", 5: "subu", 6: "addc", 7: "subc", 12: "cmphs", 13: "cmplt",
              14: "tst", 15: "cmpne", 10: "movf", 11: "lsr"}.get(sub)
        if op:
            return Instruction(op, (x, y))
    if high == 1:
        if sub in (0, 1):
            return Instruction("mfcr", (x, imm))
        if sub in (8, 9):
            return Instruction("mtcr", (x, imm))
        op = {2: "mov", 3: "bgenr", 4: "rsub", 5: "ixw", 6: "and", 7: "xor", 12: "addu",
              13: "ixh", 14: "or", 15: "andn", 10: "asr", 11: "lsl"}.get(sub)
        if op:
            return Instruction(op, (x, y))
    if high == 2:
        if sub == 12 and y == 1:
            return Instruction("divu", (x,))
        op = {0: "addi", 1: "cmplti", 2: "subi", 4: "rsubi", 5: "cmpnei", 7: "andi"}.get(sub >> 1)
        if op:
            return Instruction(op, (x, imm + 1 if op in ("addi", "cmplti", "subi") else imm))
        if sub in (12, 13) and (sub == 13 or y == 0 or y >= 8):
            return Instruction("bmaski", (x, imm or 32))
    if high == 3:
        if sub == 2 and y == 1:
            return Instruction("divs", (x,))
        if sub in (0, 1):
            return Instruction("bclri", (x, imm))
        if sub == 3 or (sub == 2 and y >= 7):
            return Instruction("bgeni", (x, imm))
        if sub in (4, 5):
            return Instruction("bseti", (x, imm))
        if sub in (6, 7):
            return Instruction("btsti", (x, imm))
        if sub >= 8:
            group = sub >> 1
            if imm == 0:
                return Instruction({4: "xsr", 5: "asrc", 6: "lslc", 7: "lsrc"}[group], (x,))
            return Instruction({4: "rotli", 5: "asri", 6: "lsli", 7: "lsri"}[group], (x, imm))
    if high == 6:
        if sub < 8:
            return Instruction("movi", (x, (word >> 4) & 127))
        if sub == 8:
            return Instruction("mulsh", (x, y))
        if sub == 11:
            return Instruction("cprcr", (word & 7, (word >> 3) & 31))
        if sub == 15:
            return Instruction("cpwcr", (word & 7, (word >> 3) & 31))
    if high == 7:
        literal = (pc + 2 + (word & 255) * 4) & 0xfffffffc
        return Instruction("jmpi" if sub == 0 else "jsri" if sub == 15 else "lrw",
                           (literal,) if sub in (0, 15) else (sub, literal))
    if 8 <= high <= 13:
        size = {8: 4, 9: 4, 10: 1, 11: 1, 12: 2, 13: 2}[high]
        return Instruction("store" if high & 1 else "load", (sub, x, y * size, size))
    if high >= 14:
        target = (pc + 2 + signed(word & 0x7ff, 11) * 2) & MASK
        op = ("bt" if sub < 8 else "bf") if high == 14 else ("br" if sub < 8 else "bsr")
        return Instruction(op, (target,))
    return Instruction("unsupported", (f"0x{word:04x}",))


class CPU:
    def __init__(self, bus, entry):
        self.bus, self.pc = bus, entry
        self.regs = [0] * 16
        self.alternate = [0] * 16
        self.cr = [0] * 32
        self.cr[0] = 0x80000000  # boot shim starts in supervisor mode, interrupts disabled
        self.c = 0
        self.cp = 0
        self.steps = 0
        self.idly4 = 0
        self.interrupt_count = 0
        self.mmu = MMU()
        self.last_instruction = (entry, None, None)

    def read(self, address, size=4, fetch=False):
        physical = self.mmu.translate(address, supervisor=bool(self.cr[0] & 0x80000000)) if self.cr[18] & 1 else address
        return self.bus.read(physical, size, fetch)

    def write(self, address, value, size=4):
        physical = self.mmu.translate(address, write=True, supervisor=bool(self.cr[0] & 0x80000000)) if self.cr[18] & 1 else address
        self.bus.write(physical, value, size)

    def fail(self, kind, message):
        raise EmulationStop(kind, f"pc=0x{self.pc:08x}: {message}")

    def control(self, number, value=None):
        # Plain register/cache state only. No fabricated MMU or exception execution.
        if number not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 17, 18, 19, 30):
            self.fail("unsupported-control-register", f"cr{number}")
        if value is None:
            return (self.cr[0] & ~1) | self.c if number == 0 else self.cr[number]
        if number == 0:
            if value & 0xe000:
                self.fail("unsupported-psr-write", "trace modes/pending trace exceptions are not implemented")
            if (value ^ self.cr[0]) & 2:
                self.regs, self.alternate = self.alternate, self.regs
            self.c = value & 1
            self.cp = (value >> 24) & 15
        self.cr[number] = value & (~0x3ff if number == 1 else MASK)

    def step(self):
        self.last_instruction = (self.pc, None, None)
        if self.interrupt_ready():
            self._enter_irq()
            self.last_instruction = (self.pc, None, None)
        word = self.read(self.pc, 2, fetch=True)
        ins = decode(word, self.pc)
        self.last_instruction = (self.pc, word, ins)
        op, a = ins.op, ins.args
        next_pc = (self.pc + 2) & MASK
        r = self.regs
        if op in ("mfcr", "mtcr", "cpseti", "cpwcr", "cprcr", "psrset", "psrclr", "rte", "rfi", "stop", "wait", "doze") and not self.cr[0] & 0x80000000:
            self.fail("privilege-exception", "privileged instruction in user mode; exception entry is not implemented")
        if op in ("bkpt", "stop", "wait", "doze"):
            self.fail("halt", op)
        elif op == "sync":
            pass
        elif op == "idly4":
            if not self.idly4:
                self.idly4 = 4
                self.c = 0
        elif op in ('rte', 'rfi'):
            psr, pc = (2, 4) if op == 'rte' else (3, 5)
            self.control(0, self.cr[psr] & ~(0x7f << 16))
            next_pc = self.cr[pc]
            self.idly4 = 0
        elif op == "cpseti":
            self.cp = a[0]
            self.cr[0] = (self.cr[0] & ~0x0f000000) | (self.cp << 24)
        elif op in ("cpwcr", "cprcr"):
            if self.cp != 15:
                self.fail("unsupported-coprocessor", f"{op} cp{self.cp}, register={a[1]}")
            if op == "cpwcr":
                self.mmu.register(a[1], r[a[0]])
            else:
                r[a[0]] = self.mmu.register(a[1])
        elif op in ("psrclr", "psrset"):
            mask = (0x40 if a[0] & 1 else 0) | (0x10 if a[0] & 2 else 0) | (0x100 if a[0] & 4 else 0)
            if not a[0]:
                mask = 2
                enabled = bool(self.cr[0] & 2)
                if enabled != (op == "psrset"):
                    self.regs, self.alternate = self.alternate, self.regs
            self.cr[0] = self.cr[0] | mask if op == "psrset" else self.cr[0] & ~mask
        elif op == "mfcr":
            r[a[0]] = self.control(a[1])
        elif op == "mtcr":
            self.control(a[1], r[a[0]])
        elif op == "movi":
            r[a[0]] = a[1]
        elif op == "lrw":
            r[a[0]] = self.read(a[1])
        elif op in ("jmpi", "jsri", "jmp", "jsr"):
            target = self.read(a[0]) if op in ("jmpi", "jsri") else r[a[0]]
            if op in ("jsri", "jsr"):
                r[15] = next_pc
            next_pc = target & ~1
        elif op in ("br", "bsr", "bt", "bf"):
            if op == "bsr":
                r[15] = next_pc
            if op in ("br", "bsr") or (op == "bt" and self.c) or (op == "bf" and not self.c):
                next_pc = a[0]
        elif op in ("load", "store"):
            z, x, offset, size = a
            address = (r[x] + offset) & MASK
            if op == "load":
                r[z] = self.read(address, size)
            else:
                self.write(address, r[z], size)
        elif op in ("ldq", "stq", "ldm", "stm"):
            registers = range(4, 8) if op in ("ldq", "stq") else range(a[0], 16)
            address = r[a[0]] if op in ("ldq", "stq") else r[0]
            if op in ("ldq", "ldm"):
                values = [self.read((address + i * 4) & MASK) for i, _ in enumerate(registers)]
                for register, value in zip(registers, values):
                    r[register] = value
            else:
                for i, register in enumerate(registers):
                    self.write((address + i * 4) & MASK, r[register])
        elif op in ("divu", "divs"):
            if r[1] == 0:
                self.fail("divide-by-zero", "division exception handling is not implemented")
            if op == "divu":
                r[a[0]] //= r[1]
            else:
                numerator, denominator = signed(r[a[0]]), signed(r[1])
                quotient = abs(numerator) // abs(denominator)
                r[a[0]] = -quotient if (numerator < 0) != (denominator < 0) else quotient
        elif len(a) == 2 and op in ("mov", "movt", "movf", "mult", "subu", "rsub", "ixw", "and", "xor",
                                   "addu", "ixh", "or", "andn", "cmphs", "cmplt", "tst", "cmpne",
                                   "lsl", "lsr", "asr", "bgenr", "addc", "subc", "mulsh"):
            x, y = a
            if op == "mov" or (op == "movt" and self.c) or (op == "movf" and not self.c): r[x] = r[y]
            elif op in ("movt", "movf"): pass
            elif op == "mult": r[x] *= r[y]
            elif op == "subu": r[x] -= r[y]
            elif op == "rsub": r[x] = r[y] - r[x]
            elif op == "ixw": r[x] += r[y] * 4
            elif op == "ixh": r[x] += r[y] * 2
            elif op == "addu": r[x] += r[y]
            elif op == "and": r[x] &= r[y]
            elif op == "xor": r[x] ^= r[y]
            elif op == "or": r[x] |= r[y]
            elif op == "andn": r[x] &= ~r[y]
            elif op == "cmphs": self.c = int(r[x] >= r[y])
            elif op == "cmplt": self.c = int(signed(r[x]) < signed(r[y]))
            elif op == "tst": self.c = int(bool(r[x] & r[y]))
            elif op == "cmpne": self.c = int(r[x] != r[y])
            elif op == "addc":
                result = r[x] + r[y] + self.c
                r[x], self.c = result & MASK, int(result > MASK)
            elif op == "subc":
                result = r[x] - r[y] - (1 - self.c)
                r[x], self.c = result & MASK, int(result >= 0)
            elif op == "mulsh": r[x] = signed(r[x], 16) * signed(r[y], 16)
            elif op == "bgenr": r[x] = 0 if r[y] & 32 else 1 << (r[y] & 31)
            elif op in ("lsl", "lsr", "asr"):
                shift = r[y] & 63
                if op == "asr": r[x] = signed(r[x]) >> min(shift, 31)
                elif shift > 31: r[x] = 0
                elif op == "lsl": r[x] <<= shift
                else: r[x] >>= shift
        elif len(a) == 2 and op in ("addi", "cmplti", "subi", "rsubi", "cmpnei", "andi", "bmaski", "bclri",
                                   "bgeni", "bseti", "btsti", "rotli", "asri", "lsli", "lsri"):
            x, imm = a
            if op == "addi": r[x] += imm
            elif op == "subi": r[x] -= imm
            elif op == "rsubi": r[x] = imm - r[x]
            elif op == "cmplti": self.c = int(signed(r[x]) < imm)
            elif op == "cmpnei": self.c = int(r[x] != imm)
            elif op == "andi": r[x] &= imm
            elif op == "bmaski": r[x] = (1 << imm) - 1
            elif op == "bclri": r[x] &= ~(1 << imm)
            elif op == "bgeni": r[x] = 1 << imm
            elif op == "bseti": r[x] |= 1 << imm
            elif op == "btsti": self.c = (r[x] >> imm) & 1
            elif op == "rotli": r[x] = (r[x] << imm) | (r[x] >> (32 - imm))
            elif op == "asri": r[x] = signed(r[x]) >> imm
            elif op == "lsli": r[x] <<= imm
            elif op == "lsri": r[x] >>= imm
        elif len(a) == 1 and op in ("mvc", "mvcv", "dect", "decf", "inct", "incf", "zextb", "sextb", "zexth", "sexth",
                                   "declt", "decgt", "decne", "clrt", "clrf", "abs", "not", "asrc", "lslc", "lsrc", "xsr",
                                   "ff1", "brev", "tstnbz", "xtrb0", "xtrb1", "xtrb2", "xtrb3"):
            x = a[0]
            if op == "mvc": r[x] = self.c
            elif op == "mvcv": r[x] = 1 - self.c
            elif op == "dect": r[x] -= self.c
            elif op == "decf": r[x] -= 1 - self.c
            elif op == "inct": r[x] += self.c
            elif op == "incf": r[x] += 1 - self.c
            elif op == "zextb": r[x] &= 255
            elif op == "sextb": r[x] = signed(r[x], 8)
            elif op == "zexth": r[x] &= 65535
            elif op == "sexth": r[x] = signed(r[x], 16)
            elif op in ("declt", "decgt", "decne"):
                r[x] = (r[x] - 1) & MASK
                self.c = int(signed(r[x]) < 0 if op == "declt" else signed(r[x]) > 0 if op == "decgt" else r[x] != 0)
            elif op == "clrt" and self.c: r[x] = 0
            elif op == "clrf" and not self.c: r[x] = 0
            elif op in ("clrt", "clrf"): pass
            elif op == "abs": r[x] = abs(signed(r[x]))
            elif op == "not": r[x] = ~r[x]
            elif op in ("asrc", "lslc", "lsrc", "xsr"):
                old, carry = r[x], self.c
                self.c = (old >> 31) if op == "lslc" else old & 1
                r[x] = old << 1 if op == "lslc" else signed(old) >> 1 if op == "asrc" else (old >> 1) | (carry << 31) if op == "xsr" else old >> 1
            elif op == "ff1": r[x] = 32 - r[x].bit_length()
            elif op == "brev": r[x] = int(f"{r[x]:032b}"[::-1], 2)
            elif op == "tstnbz": self.c = int(all((r[x] >> shift) & 255 for shift in (0, 8, 16, 24)))
            elif op.startswith("xtrb"):
                r[1] = (r[x] >> ((3 - int(op[-1])) * 8)) & 255
                self.c = int(r[1] != 0)
        else:
            self.fail("unsupported-instruction", str(ins))
        self.regs[:] = [value & MASK for value in self.regs]
        self.pc = next_pc
        self.steps += 1
        self.bus.ticks += 1
        if op != "idly4" and self.idly4:
            self.idly4 -= 1
        return word, ins

    def interrupt_ready(self):
        controller = self.bus.interrupt_masks
        supported = 0
        if self.bus.timer_interrupts and self.bus.timers is not None:
            supported |= 1 << 10
            controller.sources = (controller.sources & ~(1 << 10)) | (int(self.bus.timers.irq_pending(0)) << 10)
        if self.bus.graphics is not None:
            supported |= 1 << 62
            controller.sources = (controller.sources & ~(1 << 62)) | (int(self.bus.graphics.irq_pending()) << 62)
        if self.bus.infrared is not None:
            supported |= 1 << 4
            controller.sources = (controller.sources & ~(1 << 4)) | (int(self.bus.infrared.irq_pending()) << 4)
        enabled = ((controller.registers.get(0x40, 0) & ~controller.registers[0x50]) |
                   ((controller.registers.get(0x44, 0) & ~controller.registers[0x54]) << 32))
        return bool(enabled & controller.routed_sources(sources=controller.sources & supported) and
                    self.cr[0] & 0x140 == 0x140 and not self.idly4)

    def interrupt_budget(self, count):
        if not self.bus.timer_interrupts or self.bus.timers is None or self.cr[0] & 0x140 != 0x140:
            return count
        controller = self.bus.interrupt_masks
        enabled = ((controller.registers.get(0x40, 0) & ~controller.registers[0x50]) |
                   ((controller.registers.get(0x44, 0) & ~controller.registers[0x54]) << 32))
        if not enabled & controller.routed_sources(sources=1 << 10):
            return count
        due = self.bus.timers.instructions_to_irq(0)
        return count if due is None else min(count, max(1, due))

    def _enter_irq(self):
        # ABIv1 autovectored IRQ = vector 10. Fetch the vector as supervisor
        # before committing state, so a missing vector is an explicit fault.
        vector = self.cr[1] + 10 * 4
        physical = self.mmu.translate(vector, supervisor=True) if self.cr[18] & 1 else vector
        target = self.bus.read(physical, 4, fetch=True)
        saved = (self.control(0) & ~(0x7f << 16)) | (10 << 16)
        self.cr[2], self.cr[4] = saved, self.pc
        active = (saved | 0x80000000) & ~(0xe140 | 2)
        self.control(0, active | ((target & 1) << 1))
        self.pc = target & ~1
        self.interrupt_count += 1

    def snapshot(self):
        return {"pc": f"0x{self.pc:08x}", "steps": self.steps, "carry": self.c,
                "coprocessor": self.cp, "interrupt_inhibit_steps": self.idly4,
                "interrupts_taken": self.interrupt_count,
                "registers": [f"0x{r:08x}" for r in self.regs],
                "control_registers": {str(i): f"0x{r:08x}" for i, r in enumerate(self.cr) if r}}
