#!/usr/bin/env python3
"""Build a standalone bootable C-SKY CHIP-8 Game Console firmware for GX6605S / StarSat SR-4040HD Vega."""
from pathlib import Path
import struct
from starsat_emu.cpu import decode, Instruction


class CskyAsm:
    def __init__(self, base):
        self.base = base
        self.words = []
        self.labels = {}
        self.fixups = []

    def label(self, name):
        if name in self.labels:
            raise ValueError(f"duplicate label: {name}")
        self.labels[name] = self.base + len(self.words) * 2

    def emit(self, op, *a):
        pc = self.base + len(self.words) * 2
        if op in ("lrw", "br", "bt", "bf", "bsr"):
            self.fixups.append((len(self.words), op, a))
            word = 0
        elif op in ("mov", "and", "xor", "or", "addu", "subu", "cmpne", "cmphs", "cmplt", "lsl", "lsr"):
            x, y = a
            code_map = {
                "mov": 0x1200, "subu": 0x0500, "and": 0x1600, "xor": 0x1700,
                "lsr": 0x0b00, "lsl": 0x1b00, "addu": 0x1c00, "or": 0x1e00,
                "cmphs": 0x0c00, "cmplt": 0x0d00, "cmpne": 0x0f00,
            }
            word = code_map[op] | (y << 4) | x
        elif op in ("load", "store"):
            z, x, offset, size = a
            if size not in (1, 2, 4) or offset % size or not 0 <= offset // size <= 15:
                raise ValueError("invalid load/store displacement")
            word = {("load", 4): 0x8000, ("store", 4): 0x9000,
                    ("load", 1): 0xa000, ("store", 1): 0xb000,
                    ("load", 2): 0xc000, ("store", 2): 0xd000}[op, size] | (z << 8) | ((offset // size) << 4) | x
        elif op == "movi":
            x, value = a
            if not 0 <= value <= 127:
                raise ValueError(f"movi out of range: {value}")
            word = 0x6000 | (value << 4) | x
        elif op in ("addi", "subi", "cmpnei", "cmplti", "btsti", "bseti", "bclri", "lsli", "lsri"):
            x, value = a
            imm = value - 1 if op in ("addi", "subi") else value
            if not 0 <= imm <= 31:
                raise ValueError(f"{op} imm out of range: {value}")
            code_map = {
                "addi": 0x2000, "subi": 0x2400, "cmplti": 0x2800, "cmpnei": 0x2a00,
                "bclri": 0x3000, "bseti": 0x3400, "btsti": 0x3600, "lsli": 0x3c00, "lsri": 0x3e00,
            }
            word = code_map[op] | (imm << 4) | x
        elif op == "psrclr":
            word = 0x11f0 | a[0]
        elif op in ("zextb", "not", "jmp", "jsr"):
            word = {"zextb": 0x0140, "not": 0x01f0, "jmp": 0x00c0, "jsr": 0x00e0}[op] | a[0]
        else:
            raise ValueError(f"unsupported op: {op}")
        self.words.append(word)
        if word:
            decoded = decode(word, pc)
            if decoded.op != op:
                raise AssertionError(f"decode mismatch at {hex(pc)}: {hex(word)} -> {decoded} != {op}{a}")

    def finish(self, data_blocks):
        if len(self.words) % 2:
            self.emit("mov", 0, 0)
        constants = []
        for _, op, args in self.fixups:
            if op == "lrw" and args[1] not in constants:
                constants.append(args[1])
        pool_base = self.base + len(self.words) * 2
        cursor = pool_base + len(constants) * 4
        for name, block in data_blocks.items():
            if len(block) % 4:
                raise ValueError(f"data block {name} not 4-byte aligned")
            self.labels[name] = cursor
            cursor += len(block)
        for index, op, args in self.fixups:
            pc = self.base + index * 2
            if op == "lrw":
                reg, constant = args
                address = pool_base + constants.index(constant) * 4
                disp = (address - ((pc + 2) & ~3)) // 4
                if not 0 <= disp <= 255:
                    raise ValueError(f"literal pool displacement {disp} > 255 at {hex(pc)}")
                word = 0x7000 | (reg << 8) | disp
            else:
                target = self.labels[args[0]]
                disp = (target - pc - 2) // 2
                if not -1024 <= disp <= 1023:
                    raise ValueError(f"branch {args[0]} disp {disp} out of range at {hex(pc)}")
                word = {"br": 0xf000, "bsr": 0xf800, "bt": 0xe000, "bf": 0xe800}[op] | (disp & 0x7ff)
            self.words[index] = word
        code = struct.pack("<" + "H" * len(self.words), *self.words)
        for constant in constants:
            code += struct.pack("<I", self.labels[constant] if isinstance(constant, str) else constant)
        return code + b"".join(data_blocks.values())


def build_brickout_rom() -> bytes:
    """Classic CHIP-8 Brickout ROM: Left=4, Right=6, Action=5."""
    # Simple, self-contained CHIP-8 bytecode (base 0x200)
    ins = []
    labels = {}
    fixups = []

    def L(name):
        labels[name] = 0x200 + len(ins) * 2

    def op(word):
        ins.append(word)

    def jp(name):
        fixups.append((len(ins), 0x1000, name))
        ins.append(0)

    def ld_i(name):
        fixups.append((len(ins), 0xA000, name))
        ins.append(0)

    # Clear screen
    op(0x00E0)
    # Draw 3 rows of bricks (8 bricks across, 8px wide)
    ld_i("brick_spr")
    op(0x6104)  # V1 = y = 4
    L("row_loop")
    op(0x6004)  # V0 = x = 4
    L("col_loop")
    op(0xD012)  # DRW V0, V1, 2
    op(0x7007)  # V0 += 7
    op(0x303C)  # SE V0, 60
    jp("col_loop")
    op(0x7104)  # V1 += 4
    op(0x3110)  # SE V1, 16
    jp("row_loop")

    # Paddle: V2 = px = 28, V3 = py = 30
    op(0x621C)
    op(0x631E)
    ld_i("paddle_spr")
    op(0xD231)  # DRW V2, V3, 1

    # Ball: V4 = bx = 31, V5 = by = 22, V6 = dx = 1, V7 = dy = -1 (255)
    op(0x641F)
    op(0x6516)
    op(0x6601)
    op(0x67FF)
    ld_i("ball_spr")
    op(0xD451)

    L("main_loop")
    # Small frame delay via delay timer
    op(0x6802)
    op(0xF815)  # LD DT, V8
    L("wait_dt")
    op(0xF807)  # LD V8, DT
    op(0x3800)  # SE V8, 0
    jp("wait_dt")

    # Input: Key 4 (Left), Key 6 (Right)
    op(0x6804)
    op(0xE89E)  # SKP V8
    jp("chk_right")
    op(0x3202)  # SE V2, 2
    jp("move_left")
    jp("chk_right")
    L("move_left")
    ld_i("paddle_spr")
    op(0xD231)
    op(0x72FE)  # V2 -= 2
    op(0xD231)

    L("chk_right")
    op(0x6806)
    op(0xE89E)  # SKP V8
    jp("move_ball")
    op(0x3236)  # SE V2, 54
    jp("move_right")
    jp("move_ball")
    L("move_right")
    ld_i("paddle_spr")
    op(0xD231)
    op(0x7202)  # V2 += 2
    op(0xD231)

    L("move_ball")
    ld_i("ball_spr")
    op(0xD451)  # Erase old ball
    op(0x8464)  # V4 += V6
    op(0x8574)  # V5 += V7

    # Bounce X walls (0 or 63)
    op(0x4400)  # SNE V4, 0
    op(0x6601)
    op(0x443F)  # SNE V4, 63
    op(0x66FF)

    # Bounce top (0) or paddle (y == 30)
    op(0x4500)  # SNE V5, 0
    op(0x6701)
    op(0x451E)  # SNE V5, 30
    op(0x67FF)
    op(0x451F)  # SNE V5, 31 -> reset ball to y=20
    op(0x6514)

    ld_i("ball_spr")
    op(0xD451)  # Draw new ball
    # If VF == 1 (hit brick), bounce dy
    op(0x3F01)
    jp("main_loop")
    op(0x68FF)
    op(0x8782)  # XOR? Better: if V5 < 20 flip dy
    op(0x6914)
    op(0x8955)  # SUB V9, V5 -> VF=1 if V9 >= V5 (y <= 20)
    op(0x3F01)
    jp("main_loop")
    op(0x6701)  # bounce down
    jp("main_loop")

    L("brick_spr")
    op(0xFCFC)
    L("paddle_spr")
    op(0xFF00)
    L("ball_spr")
    op(0x8000)

    for idx, opcode, name in fixups:
        ins[idx] = opcode | (labels[name] & 0x0FFF)
    raw = b"".join(struct.pack(">H", w) for w in ins)
    return raw.ljust((len(raw) + 3) & ~3, b"\x00")


def build_firmware():
    # Base 0x100000 (boot SRAM)
    a = CskyAsm(0x100000)

    # Peripheral & memory addresses
    OSD_DESC = 0x10010000
    FB_BASE = 0x10100000
    C8_RAM = 0x10020000
    C8_STATE = 0x10022000  # V0..VF (64B), I(+64), PC(+68), SP(+72), DT(+76), GAME(+80), KEY(+84), TTL(+88), DIRTY(+92)
    C8_STACK = 0x10022100
    C8_DISP = 0x10023000

    VPU_BASE = 0x04800000
    CIR_BASE = 0x00204000
    OHCI_BASE = 0x00a00000

    # --- Entry point ---
    a.emit("psrclr", 7)
    a.emit("lrw", 1, 0x10040000)  # Stack in DRAM (safe in emulator and after DRAM init)
    a.emit("mov", 0, 1)

    # Initialize VPU OSD (640x360 ARGB8888, 1:1)
    a.emit("lrw", 2, OSD_DESC)
    a.emit("lrw", 3, 0x04001C00)  # fmt=7 (32bpp), argb=1 (bit 26)
    a.emit("store", 3, 2, 0, 4)
    a.emit("movi", 3, 0)
    a.emit("store", 3, 2, 4, 4)   # palette = 0
    a.emit("lrw", 3, (639 << 16))
    a.emit("store", 3, 2, 8, 4)   # horiz = 0..639
    a.emit("lrw", 3, (359 << 16))
    a.emit("store", 3, 2, 12, 4)  # vert = 0..359
    a.emit("lrw", 3, FB_BASE)
    a.emit("store", 3, 2, 16, 4)  # pixels
    a.emit("movi", 3, 0)
    a.emit("store", 3, 2, 20, 4)  # next = 0
    a.emit("lrw", 3, 0x80000000 | 640)
    a.emit("store", 3, 2, 24, 4)  # LIST_END + stride 640

    a.emit("lrw", 4, VPU_BASE)
    a.emit("lrw", 3, 0x80300001)  # SYS_PARA order=3
    a.emit("store", 3, 4, 0xe8 - 0x00, 4) if False else None
    # Note: store disp max is 60 (15*4), so add offset to r4
    a.emit("lrw", 5, VPU_BASE + 0x90)
    a.emit("store", 2, 5, 4, 4)   # 0x94 = OSD_DESC
    a.emit("lrw", 3, (360 << 16) | 640)
    a.emit("store", 3, 5, 8, 4)   # 0x98 = 640x360
    a.emit("lrw", 3, 0x10001000)
    a.emit("store", 3, 5, 12, 4)  # 0x9c = 1:1 zoom
    a.emit("movi", 3, 0)
    a.emit("store", 3, 5, 28, 4)  # 0xac = 0 offset
    a.emit("lrw", 6, VPU_BASE + 0xe8)
    a.emit("lrw", 3, 0x80300001)
    a.emit("store", 3, 6, 0, 4)   # 0xe8 = SYS_PARA
    a.emit("movi", 3, 1)
    a.emit("store", 3, 5, 0, 4)   # 0x90 = enable OSD

    # Enable CIR infrared receiver (0x00204000)
    a.emit("lrw", 4, CIR_BASE)
    a.emit("movi", 3, 1)
    a.emit("store", 3, 4, 0, 4)

    # Clear 640x360 framebuffer to #0F172A (0xFF0F172A) and draw cyan frame
    a.emit("lrw", 2, FB_BASE)
    a.emit("lrw", 3, 640 * 360)
    a.emit("lrw", 4, 0xFF0F172A)
    a.label("bg_loop")
    a.emit("store", 4, 2, 0, 4)
    a.emit("addi", 2, 4)
    a.emit("subi", 3, 1)
    a.emit("cmpnei", 3, 0)
    a.emit("bt", "bg_loop")

    # Draw top/bottom cyan border around 512x256 playfield (x=62..577, y=50..309)
    a.emit("lrw", 4, 0xFF38BDF8)
    a.emit("lrw", 2, FB_BASE + (50 * 640 + 62) * 4)
    a.emit("lrw", 5, FB_BASE + (309 * 640 + 62) * 4)
    a.emit("lrw", 3, 516)
    a.label("border_h")
    a.emit("store", 4, 2, 0, 4)
    a.emit("store", 4, 5, 0, 4)
    a.emit("addi", 2, 4)
    a.emit("addi", 5, 4)
    a.emit("subi", 3, 1)
    a.emit("cmpnei", 3, 0)
    a.emit("bt", "border_h")

    # Load ROM into C8_RAM + 0x200
    a.label("reset_vm")
    a.emit("lrw", 2, C8_RAM)
    a.emit("lrw", 3, 1024)
    a.emit("movi", 4, 0)
    a.label("clr_ram")
    a.emit("store", 4, 2, 0, 4)
    a.emit("addi", 2, 4)
    a.emit("subi", 3, 1)
    a.emit("cmpnei", 3, 0)
    a.emit("bt", "clr_ram")

    a.emit("lrw", 2, "rom_brickout")
    a.emit("lrw", 3, C8_RAM + 0x200)
    a.emit("lrw", 5, 128)  # 512 bytes max
    a.label("copy_rom")
    a.emit("load", 4, 2, 0, 4)
    a.emit("store", 4, 3, 0, 4)
    a.emit("addi", 2, 4)
    a.emit("addi", 3, 4)
    a.emit("subi", 5, 1)
    a.emit("cmpnei", 5, 0)
    a.emit("bt", "copy_rom")

    # Clear C8_STATE and C8_DISP
    a.emit("lrw", 2, C8_STATE)
    a.emit("movi", 3, 32)
    a.emit("movi", 4, 0)
    a.label("clr_state")
    a.emit("store", 4, 2, 0, 4)
    a.emit("addi", 2, 4)
    a.emit("subi", 3, 1)
    a.emit("cmpnei", 3, 0)
    a.emit("bt", "clr_state")

    a.emit("lrw", 2, C8_STATE)
    a.emit("lrw", 3, 0x200)
    a.emit("store", 3, 2, 68 - 0x40 + 64, 4) if False else None
    # Use r7 = C8_STATE + 64 for I(0), PC(4), SP(8), DT(12), GAME(16), KEY(20), TTL(24), DIRTY(28)
    a.emit("lrw", 7, C8_STATE + 64)
    a.emit("lrw", 3, 0x200)
    a.emit("store", 3, 7, 4, 4)   # PC = 0x200
    a.emit("lrw", 3, 0xFF)
    a.emit("store", 3, 7, 20, 4)  # KEY = 0xFF
    a.emit("movi", 3, 1)
    a.emit("store", 3, 7, 28, 4)  # DIRTY = 1

    # --- Main VM + Frame Loop ---
    a.label("frame_loop")
    a.emit("bsr", "poll_input")
    # Run 12 CHIP-8 opcodes per frame
    a.emit("movi", 13, 12)
    a.label("vm_step_loop")
    a.emit("bsr", "step_chip8")
    a.emit("subi", 13, 1)
    a.emit("cmpnei", 13, 0)
    a.emit("bt", "vm_step_loop")

    # Decrement delay timer if > 0
    a.emit("lrw", 7, C8_STATE + 64)
    a.emit("load", 3, 7, 12, 4)
    a.emit("cmpnei", 3, 0)
    a.emit("bf", "skip_dt")
    a.emit("subi", 3, 1)
    a.emit("store", 3, 7, 12, 4)
    a.label("skip_dt")

    # Decrement key TTL if > 0
    a.emit("load", 3, 7, 24, 4)
    a.emit("cmpnei", 3, 0)
    a.emit("bf", "skip_ttl")
    a.emit("subi", 3, 1)
    a.emit("store", 3, 7, 24, 4)
    a.emit("cmpnei", 3, 0)
    a.emit("bt", "skip_ttl")
    a.emit("lrw", 3, 0xFF)
    a.emit("store", 3, 7, 20, 4)
    a.label("skip_ttl")

    # Render OSD if DIRTY != 0
    a.emit("load", 3, 7, 28, 4)
    a.emit("cmpnei", 3, 0)
    a.emit("bf", "frame_loop")
    a.emit("movi", 3, 0)
    a.emit("store", 3, 7, 28, 4)
    a.emit("bsr", "render_osd")
    a.emit("br", "frame_loop")

    # --- poll_input: read CIR NEC FIFO (0x00204000) & OHCI port (0x00400054) ---
    a.label("poll_input")
    a.emit("lrw", 2, CIR_BASE)
    a.emit("load", 3, 2, 4, 4)   # status
    a.emit("lsri", 3, 3)
    a.emit("lrw", 4, 0x3f)
    a.emit("and", 3, 4)
    a.emit("lrw", 4, 33)
    a.emit("cmplt", 3, 4)
    a.emit("bt", "poll_ohci")
    # Read 33 words: word 0 = header, words 1..32 = 32 NEC bits (MSB first, <=1685 -> 1)
    a.emit("load", 3, 2, 8, 4)   # discard header
    a.emit("movi", 5, 0)         # decoded code32
    a.emit("movi", 6, 32)
    a.emit("lrw", 8, 1685)
    a.label("nec_bit_loop")
    a.emit("load", 3, 2, 8, 4)
    a.emit("lsri", 3, 16)
    a.emit("lsli", 5, 1)
    a.emit("cmplt", 8, 3)        # 1685 < high16 -> bit 0
    a.emit("bt", "nec_bit_zero")
    a.emit("bseti", 5, 0)
    a.label("nec_bit_zero")
    a.emit("subi", 6, 1)
    a.emit("cmpnei", 6, 0)
    a.emit("bt", "nec_bit_loop")
    # Clear CIR IRQ
    a.emit("movi", 3, 2)
    a.emit("store", 3, 2, 4, 4)
    # Extract key_byte = (code32 >> 8) & 0xff
    a.emit("lsri", 5, 8)
    a.emit("zextb", 5)
    # Map StarSat NEC key_byte -> CHIP-8 key (LEFT 0x77->4, RIGHT 0xf7->6, UP 0x2f->2, DOWN 0x8f->8, OK 0x4f->5, MENU 0x97->reset)
    a.emit("lrw", 7, C8_STATE + 64)
    a.emit("lrw", 4, 0x77)
    a.emit("cmpne", 5, 4)
    a.emit("bt", "not_left")
    a.emit("movi", 3, 4)
    a.emit("br", "set_key")
    a.label("not_left")
    a.emit("lrw", 4, 0xf7)
    a.emit("cmpne", 5, 4)
    a.emit("bt", "not_right")
    a.emit("movi", 3, 6)
    a.emit("br", "set_key")
    a.label("not_right")
    a.emit("lrw", 4, 0x2f)
    a.emit("cmpne", 5, 4)
    a.emit("bt", "not_up")
    a.emit("movi", 3, 2)
    a.emit("br", "set_key")
    a.label("not_up")
    a.emit("lrw", 4, 0x8f)
    a.emit("cmpne", 5, 4)
    a.emit("bt", "not_down")
    a.emit("movi", 3, 8)
    a.emit("br", "set_key")
    a.label("not_down")
    a.emit("lrw", 4, 0x4f)
    a.emit("cmpne", 5, 4)
    a.emit("bt", "not_ok")
    a.emit("movi", 3, 5)
    a.emit("br", "set_key")
    a.label("not_ok")
    a.emit("lrw", 4, 0x97)
    a.emit("cmpne", 5, 4)
    a.emit("bt", "poll_ohci")
    a.emit("br", "reset_vm")
    a.label("set_key")
    a.emit("store", 3, 7, 20, 4)
    a.emit("movi", 3, 18)
    a.emit("store", 3, 7, 24, 4)
    a.label("poll_ohci")
    # Touch OHCI0 RhPortStatus[0] cleanly for wireless USB dongle presence check
    a.emit("lrw", 2, OHCI_BASE + 0x54)
    a.emit("load", 3, 2, 0, 4)
    a.emit("jmp", 15)

    # --- step_chip8: fetch & execute 1 CHIP-8 opcode ---
    a.label("step_chip8")
    a.emit("lrw", 7, C8_STATE + 64)
    a.emit("load", 2, 7, 4, 4)   # PC
    a.emit("lrw", 3, C8_RAM)
    a.emit("addu", 3, 2)
    a.emit("load", 4, 3, 0, 1)   # hi
    a.emit("load", 5, 3, 1, 1)   # lo
    a.emit("lsli", 4, 8)
    a.emit("or", 4, 5)           # r4 = opcode (16-bit)
    a.emit("addi", 2, 2)
    a.emit("store", 2, 7, 4, 4)   # PC += 2

    # Decode top nibble (r6 = opcode >> 12)
    a.emit("mov", 6, 4)
    a.emit("lsri", 6, 12)

    # 00E0: CLS
    a.emit("lrw", 8, 0x00E0)
    a.emit("cmpne", 4, 8)
    a.emit("bt", "not_cls")
    a.emit("lrw", 2, C8_DISP)
    a.emit("lrw", 3, 512)
    a.emit("movi", 5, 0)
    a.label("cls_loop")
    a.emit("store", 5, 2, 0, 4)
    a.emit("addi", 2, 4)
    a.emit("subi", 3, 1)
    a.emit("cmpnei", 3, 0)
    a.emit("bt", "cls_loop")
    a.emit("movi", 5, 1)
    a.emit("store", 5, 7, 28, 4)
    a.emit("jmp", 15)

    a.label("not_cls")
    # 1nnn: JP
    a.emit("cmpnei", 6, 1)
    a.emit("bt", "not_1nnn")
    a.emit("lrw", 8, 0x0FFF)
    a.emit("and", 4, 8)
    a.emit("store", 4, 7, 4, 4)
    a.emit("jmp", 15)

    a.label("not_1nnn")
    # 3xkk: SE Vx, kk
    a.emit("cmpnei", 6, 3)
    a.emit("bt", "not_3xkk")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)
    a.emit("load", 2, 3, 0, 4)
    a.emit("zextb", 4)
    a.emit("cmpne", 2, 4)
    a.emit("bt", "ret_op")
    a.emit("load", 2, 7, 4, 4)
    a.emit("addi", 2, 2)
    a.emit("store", 2, 7, 4, 4)
    a.emit("jmp", 15)

    a.label("not_3xkk")
    # 4xkk: SNE Vx, kk
    a.emit("cmpnei", 6, 4)
    a.emit("bt", "not_4xkk")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)
    a.emit("load", 2, 3, 0, 4)
    a.emit("zextb", 4)
    a.emit("cmpne", 2, 4)
    a.emit("bf", "ret_op")
    a.emit("load", 2, 7, 4, 4)
    a.emit("addi", 2, 2)
    a.emit("store", 2, 7, 4, 4)
    a.emit("jmp", 15)

    a.label("not_4xkk")
    # 6xkk: LD Vx, kk
    a.emit("cmpnei", 6, 6)
    a.emit("bt", "not_6xkk")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)
    a.emit("zextb", 4)
    a.emit("store", 4, 3, 0, 4)
    a.emit("jmp", 15)

    a.label("not_6xkk")
    # 7xkk: ADD Vx, kk
    a.emit("cmpnei", 6, 7)
    a.emit("bt", "not_7xkk")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)
    a.emit("load", 2, 3, 0, 4)
    a.emit("zextb", 4)
    a.emit("addu", 2, 4)
    a.emit("zextb", 2)
    a.emit("store", 2, 3, 0, 4)
    a.emit("jmp", 15)

    a.label("not_7xkk")
    # 8xy4 / 8xy5 (ADD/SUB Vy)
    a.emit("cmpnei", 6, 8)
    a.emit("bt", "not_8xy")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)        # &Vx
    a.emit("mov", 5, 4)
    a.emit("lsri", 5, 4)
    a.emit("and", 5, 8)
    a.emit("lsli", 5, 2)
    a.emit("lrw", 9, C8_STATE)
    a.emit("addu", 9, 5)        # &Vy
    a.emit("load", 2, 3, 0, 4)  # Vx
    a.emit("load", 5, 9, 0, 4)  # Vy
    a.emit("and", 4, 8)         # low nibble
    a.emit("cmpnei", 4, 4)
    a.emit("bt", "not_8xy4")
    a.emit("addu", 2, 5)
    a.emit("zextb", 2)
    a.emit("store", 2, 3, 0, 4)
    a.emit("jmp", 15)
    a.label("not_8xy4")
    a.emit("cmpnei", 4, 5)
    a.emit("bt", "ret_op")
    a.emit("movi", 8, 0)
    a.emit("cmphs", 2, 5)
    a.emit("bf", "sub_nof")
    a.emit("movi", 8, 1)
    a.label("sub_nof")
    a.emit("subu", 2, 5)
    a.emit("zextb", 2)
    a.emit("store", 2, 3, 0, 4)
    a.emit("lrw", 9, C8_STATE + 60)
    a.emit("store", 8, 9, 0, 4)  # VF
    a.emit("jmp", 15)

    a.label("not_8xy")
    # Annn: LD I, nnn
    a.emit("cmpnei", 6, 10)
    a.emit("bt", "not_Annn")
    a.emit("lrw", 8, 0x0FFF)
    a.emit("and", 4, 8)
    a.emit("store", 4, 7, 0, 4)
    a.emit("jmp", 15)

    a.label("not_Annn")
    # Dxyn: DRW Vx, Vy, n
    a.emit("cmpnei", 6, 13)
    a.emit("bt", "not_Dxyn")
    a.emit("lrw", 8, 0x0F)
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 2, 3)
    a.emit("load", 2, 2, 0, 4)  # r2 = x
    a.emit("mov", 5, 4)
    a.emit("lsri", 5, 4)
    a.emit("and", 5, 8)
    a.emit("lsli", 5, 2)
    a.emit("addu", 5, 3)
    a.emit("load", 5, 5, 0, 4)  # r5 = y
    a.emit("and", 4, 8)         # r4 = n rows
    a.emit("lrw", 9, C8_STATE + 60)
    a.emit("movi", 8, 0)
    a.emit("store", 8, 9, 0, 4)  # VF = 0
    a.emit("load", 10, 7, 0, 4)  # r10 = I
    a.emit("lrw", 11, C8_RAM)
    a.emit("addu", 10, 11)       # r10 = &RAM[I]
    a.emit("movi", 12, 0)        # row = 0
    a.label("drw_row")
    a.emit("cmpne", 12, 4)
    a.emit("bf", "drw_done")
    a.emit("load", 3, 10, 0, 1)  # sprite byte
    a.emit("addi", 10, 1)
    a.emit("movi", 6, 0)         # col = 0..7
    a.label("drw_col")
    a.emit("movi", 8, 7)
    a.emit("subu", 8, 6)
    a.emit("mov", 11, 3)
    a.emit("lsr", 11, 8)
    a.emit("btsti", 11, 0)
    a.emit("bf", "drw_next_col")
    # px = (x + col) & 63, py = (y + row) & 31
    a.emit("mov", 8, 2)
    a.emit("addu", 8, 6)
    a.emit("lrw", 11, 63)
    a.emit("and", 8, 11)
    a.emit("mov", 11, 5)
    a.emit("addu", 11, 12)
    a.emit("lrw", 14, 31)
    a.emit("and", 11, 14)
    a.emit("lsli", 11, 6)
    a.emit("addu", 11, 8)
    a.emit("lrw", 8, C8_DISP)
    a.emit("addu", 8, 11)
    a.emit("load", 11, 8, 0, 1)
    a.emit("cmpnei", 11, 1)
    a.emit("bt", "drw_no_col")
    a.emit("movi", 14, 1)
    a.emit("store", 14, 9, 0, 4)  # VF = 1
    a.label("drw_no_col")
    a.emit("movi", 14, 1)
    a.emit("xor", 11, 14)
    a.emit("store", 11, 8, 0, 1)
    a.label("drw_next_col")
    a.emit("addi", 6, 1)
    a.emit("cmpnei", 6, 8)
    a.emit("bt", "drw_col")
    a.emit("addi", 12, 1)
    a.emit("br", "drw_row")
    a.label("drw_done")
    a.emit("movi", 8, 1)
    a.emit("store", 8, 7, 28, 4)  # DIRTY = 1
    a.emit("jmp", 15)

    a.label("not_Dxyn")
    # Ex9E: SKP Vx
    a.emit("cmpnei", 6, 14)
    a.emit("bt", "not_Ex")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)
    a.emit("load", 2, 3, 0, 4)
    a.emit("load", 3, 7, 20, 4)  # active_key
    a.emit("cmpne", 2, 3)
    a.emit("bt", "ret_op")
    a.emit("load", 2, 7, 4, 4)
    a.emit("addi", 2, 2)
    a.emit("store", 2, 7, 4, 4)
    a.emit("jmp", 15)

    a.label("not_Ex")
    # Fx07 / Fx15 (DT)
    a.emit("cmpnei", 6, 15)
    a.emit("bt", "ret_op")
    a.emit("mov", 2, 4)
    a.emit("lsri", 2, 8)
    a.emit("lrw", 8, 0x0F)
    a.emit("and", 2, 8)
    a.emit("lsli", 2, 2)
    a.emit("lrw", 3, C8_STATE)
    a.emit("addu", 3, 2)
    a.emit("zextb", 4)
    a.emit("cmpnei", 4, 0x07)
    a.emit("bt", "not_fx07")
    a.emit("load", 2, 7, 12, 4)
    a.emit("store", 2, 3, 0, 4)
    a.emit("jmp", 15)
    a.label("not_fx07")
    a.emit("cmpnei", 4, 0x15)
    a.emit("bt", "ret_op")
    a.emit("load", 2, 3, 0, 4)
    a.emit("store", 2, 7, 12, 4)
    a.label("ret_op")
    a.emit("jmp", 15)

    # --- render_osd: scale 64x32 C8_DISP (8x8) to FB_BASE at (64, 52) ---
    a.label("render_osd")
    a.emit("lrw", 2, C8_DISP)
    a.emit("lrw", 3, FB_BASE + (52 * 640 + 64) * 4)
    a.emit("lrw", 4, 0xFF38BDF8)  # cyan pixel
    a.emit("lrw", 5, 0xFF1E293B)  # dark slate off-pixel
    a.emit("movi", 6, 32)         # 32 CHIP-8 rows
    a.label("osd_y")
    a.emit("movi", 8, 8)          # 8x vertical scale
    a.label("osd_sy")
    a.emit("mov", 9, 2)           # row src ptr
    a.emit("mov", 10, 3)          # row dst ptr
    a.emit("lrw", 11, 64)         # 64 CHIP-8 cols
    a.label("osd_x")
    a.emit("load", 12, 9, 0, 1)
    a.emit("addi", 9, 1)
    a.emit("mov", 14, 5)
    a.emit("cmpnei", 12, 0)
    a.emit("bf", "osd_off")
    a.emit("mov", 14, 4)
    a.label("osd_off")
    # Write 8 pixels (32 bytes)
    a.emit("store", 14, 10, 0, 4)
    a.emit("store", 14, 10, 4, 4)
    a.emit("store", 14, 10, 8, 4)
    a.emit("store", 14, 10, 12, 4)
    a.emit("store", 14, 10, 16, 4)
    a.emit("store", 14, 10, 20, 4)
    a.emit("store", 14, 10, 24, 4)
    a.emit("store", 14, 10, 28, 4)
    a.emit("addi", 10, 32)
    a.emit("subi", 11, 1)
    a.emit("cmpnei", 11, 0)
    a.emit("bt", "osd_x")
    a.emit("lrw", 11, 640 * 4)
    a.emit("addu", 3, 11)
    a.emit("subi", 8, 1)
    a.emit("cmpnei", 8, 0)
    a.emit("bt", "osd_sy")
    a.emit("lrw", 11, 64)
    a.emit("addu", 2, 11)
    a.emit("subi", 6, 1)
    a.emit("cmpnei", 6, 0)
    a.emit("bt", "osd_y")
    a.emit("jmp", 15)

    rom = build_brickout_rom()
    payload_body = a.finish({"rom_brickout": rom})
    print(f"CHIP-8 firmware code+ROM size: {len(payload_body)} bytes (max 12288)")

    # Build bootable .bin with aa55aa55 at 0x100
    root = Path(__file__).resolve().parent.parent
    out_bin = root / "custom_firmware/SR-4040HD_VEGA_chip8_console.bin"
    out_bin.parent.mkdir(exist_ok=True)
    header = bytearray(0x100)
    header[:16] = b"CHIP8_GX6605S_V1"
    image = bytes(header) + b"\xaa\x55\xaa\x55" + payload_body
    image = image.ljust(0x4000, b"\xff")
    out_bin.write_bytes(image)
    print(f"Wrote {out_bin} ({len(image)} bytes)")


if __name__ == "__main__":
    build_firmware()
