"""Small ABIv1 emitter for the project's fixed, reviewable RAM diagnostics."""
import struct
from starsat_emu.cpu import decode, Instruction


class Assembler:
    def __init__(self, base):
        self.base, self.words, self.labels, self.fixups, self.expected = base, [], {}, [], []

    def label(self, name):
        if name in self.labels:
            raise ValueError("duplicate label")
        self.labels[name] = self.base + len(self.words) * 2

    def emit(self, op, *a):
        pc = self.base + len(self.words) * 2
        if op in ("lrw", "br", "bt", "bf", "bsr"):
            self.fixups.append((len(self.words), op, a))
            word = 0
        elif op in ("mov", "and", "xor", "or", "addu", "cmpne", "cmphs"):
            x, y = a
            word = {"mov": 0x1200, "and": 0x1600, "xor": 0x1700, "or": 0x1e00, "addu": 0x1c00, "cmpne": 0x0f00, "cmphs": 0x0c00}[op] | y << 4 | x
        elif op in ("load", "store"):
            z, x, offset, size = a
            if size not in (1, 2, 4) or offset % size or not 0 <= offset // size <= 15:
                raise ValueError("invalid load/store displacement")
            word = {("load", 4): 0x8000, ("store", 4): 0x9000,
                    ("load", 1): 0xa000, ("store", 1): 0xb000,
                    ("load", 2): 0xc000, ("store", 2): 0xd000}[op, size] | z << 8 | (offset // size) << 4 | x
        elif op == "movi":
            x, value = a
            if not 0 <= value <= 127:
                raise ValueError("movi requires 0..127")
            word = 0x6000 | value << 4 | x
        elif op == 'mfcr':
            x, control = a
            if not 0 <= control <= 31:raise ValueError('invalid control register')
            word = 0x1000 | control << 4 | x
        elif op == 'psrclr':
            if not 0 <= a[0] <= 7:raise ValueError('invalid PSR clear mask')
            word = 0x11f0 | a[0]
        elif op in ("addi", "subi", "cmpnei", "btsti", "bseti", "lsli", "lsri"):
            x, value = a
            immediate = value - 1 if op in ("addi", "subi") else value
            if not 0 <= immediate <= 31:
                raise ValueError("immediate outside 5-bit range")
            word = {"addi": 0x2000, "subi": 0x2400, "cmpnei": 0x2a00,
                    "btsti": 0x3600, "bseti": 0x3400, "lsli": 0x3c00, "lsri": 0x3e00}[op] | immediate << 4 | x
        elif op in ("zextb", "not", "jmp"):
            word = {"zextb": 0x0140, "not": 0x01f0, "jmp": 0x00c0}[op] | a[0]
        else:
            raise ValueError("unsupported diagnostic instruction: " + op)
        self.words.append(word)
        self.expected.append(Instruction(op, a))
        if word:
            assert decode(word, pc) == self.expected[-1], (hex(word), decode(word, pc), self.expected[-1])

    def finish(self, data):
        """Data is an ordered mapping of label -> four-byte-aligned byte blocks."""
        if len(self.words) % 2:
            self.emit("mov", 0, 0)
        constants = []
        for _, op, args in self.fixups:
            if op == "lrw" and args[1] not in constants:
                constants.append(args[1])
        pool_base = self.base + len(self.words) * 2
        cursor = pool_base + len(constants) * 4
        for name, block in data.items():
            if len(block) % 4:
                raise ValueError("data block requires alignment")
            self.labels[name] = cursor
            cursor += len(block)
        for index, op, args in self.fixups:
            pc = self.base + index * 2
            if op == "lrw":
                reg, constant = args
                address = pool_base + constants.index(constant) * 4
                displacement = (address - ((pc + 2) & ~3)) // 4
                if not 0 <= displacement <= 255:
                    raise ValueError("literal pool exceeds ABIv1 range")
                word = 0x7000 | reg << 8 | displacement
                expected = Instruction(op, (reg, address))
            else:
                target = self.labels[args[0]]
                displacement = (target - pc - 2) // 2
                if not -1024 <= displacement <= 1023:
                    raise ValueError("branch exceeds ABIv1 range")
                word = {"br": 0xf000, "bsr": 0xf800, "bt": 0xe000, "bf": 0xe800}[op] | (displacement & 0x7ff)
                expected = Instruction(op, (target,))
            assert decode(word, pc) == expected
            self.words[index] = word
        code = struct.pack("<" + "H" * len(self.words), *self.words)
        for constant in constants:
            code += struct.pack("<I", self.labels[constant] if isinstance(constant, str) else constant)
        return code + b"".join(data.values())
