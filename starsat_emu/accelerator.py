"""Optional native RAM-only interpreter, with exact Python fallback boundaries."""
import ctypes as C
import os
from pathlib import Path
from .cpu import decode
from .memory import EmulationStop


class RAM(C.Structure):
    _fields_ = [("base", C.c_uint32), ("size", C.c_uint32),
                ("data", C.POINTER(C.c_uint8)), ("readonly", C.c_uint32)]


class State(C.Structure):
    _fields_ = [("r", C.c_uint32 * 16), ("pc", C.c_uint32), ("carry", C.c_uint32),
                ("mmu", C.c_uint32), ("supervisor", C.c_uint32),
                ("msa0", C.c_uint32), ("msa1", C.c_uint32),
                ("history_pc", C.c_uint32 * 32), ("history_word", C.c_uint32 * 32),
                ("memory_writes", C.c_uint32)]


def library():
    if os.environ.get("STARSAT_PYTHON_ONLY") == "1":
        return None
    path = Path(__file__).with_name("_csky_fast.dylib")
    if not path.exists():
        return None
    native = C.CDLL(str(path))
    native.csky_run.argtypes = [C.POINTER(State), C.POINTER(RAM), C.c_uint32, C.c_uint32,
                               C.POINTER(C.c_uint32), C.c_uint32]
    native.csky_run.restype = C.c_uint32
    return native


class Accelerator:
    def __init__(self, cpu):
        self.native = library()
        self.buffers = [(C.c_uint8 * len(r.data)).from_buffer(r.data) for r in cpu.bus.regions]
        self.ram = (RAM * len(self.buffers))(*[
            RAM(r.base, len(r.data), buffer, r.readonly)
            for r, buffer in zip(cpu.bus.regions, self.buffers)])

    def advance(self, cpu, count, history, breakpoints=()):
        if self.native is None or cpu.idly4 or cpu.interrupt_ready():
            return 0
        count = cpu.interrupt_budget(count)
        polled = self._empty_i2c_poll(cpu, count, history, breakpoints)
        if polled:
            return polled
        state = State()
        state.r[:] = cpu.regs
        state.pc, state.carry = cpu.pc, cpu.c
        state.mmu = cpu.cr[18] & 1
        state.supervisor = bool(cpu.cr[0] & 0x80000000)
        state.msa0, state.msa1 = cpu.mmu.registers[30:32]
        points = (C.c_uint32 * len(breakpoints))(*breakpoints)
        done = self.native.csky_run(C.byref(state), self.ram, len(self.ram), min(count, 250000), points, len(points))
        if done:
            cpu.regs[:] = state.r
            cpu.pc, cpu.c = state.pc, state.carry
            cpu.steps += done
            cpu.bus.ticks += done
            cpu.bus.activity += state.memory_writes
            for i in range(max(0, done - 32), done):
                pc, word = state.history_pc[i & 31], state.history_word[i & 31]
                history.append((pc, word, decode(word, pc), None))
            cpu.last_instruction = history[-1][:3]
        # RAM execution commonly stops immediately before the status load.
        # Batch that boundary here, before Session's single-step fallback
        # consumes the load and places the PC back inside the RAM-only tail.
        if count > done and not cpu.interrupt_ready():
            pending = cpu.interrupt_budget(count - done)
            done += self._empty_i2c_poll(cpu, pending, history, breakpoints)
        return done

    @staticmethod
    def _empty_i2c_poll(cpu, count, history, breakpoints):
        """Batch exact iterations of the observed finite NACK wait.

        This applies only to the synchronous empty I2C model: no register can
        change during the loop. The counter, time, events, instruction history
        and interrupt deadline are preserved. The exit iteration runs normally.
        No READY/ACK value, PC skip, or shortened guest timeout is supplied.
        """
        from .i2c import I2CControllers, I2C
        if count < 8 or type(cpu.bus.i2c) is not I2CControllers:
            return 0

        def ram_value(address, size):
            if address % size:
                raise ValueError('unaligned RAM access')
            physical = cpu.mmu.translate(address, supervisor=bool(cpu.cr[0] & 0x80000000)) if cpu.cr[18] & 1 else address
            for region in cpu.bus.regions:
                offset = physical - region.base
                if 0 <= offset and offset + size <= len(region.data):
                    return int.from_bytes(region.data[offset:offset+size], 'little')
            raise ValueError('not RAM')

        pc = cpu.pc
        if (cpu.regs[6] & 0x1fffffff) in cpu.bus.i2c.bases and cpu.regs[7] > 1:
            ptr_reg, cnt_reg = 6, 7
            positions = (pc, pc+2, pc+4, pc+6, pc+8, pc+10, pc+12, pc-2)
            words = (0x8606, 0x2ea6, 0x2a86, 0xe805, 0x2407, 0x2a07, 0xe7f8, 0x8602)
        elif (cpu.regs[7] & 0x1fffffff) in cpu.bus.i2c.bases and cpu.regs[6] > 1:
            ptr_reg, cnt_reg = 7, 6
            positions = (pc, pc+2, pc+4, pc+6, pc-8, pc-6, pc-4, pc-2)
            words = (0x8707, 0x2ea7, 0x2a87, 0xe7f8, 0x2406, 0x2a06, 0xe807, 0x8702)
        else:
            return 0

        if any(point in positions for point in breakpoints):
            return 0
        try:
            if any(ram_value(address, 2) != word for address, word in zip(positions, words)):
                return 0
            pointer = ram_value(cpu.regs[2], 4)
            if pointer != cpu.regs[ptr_reg] or pointer % 4:
                return 0
            physical = cpu.mmu.translate(pointer, supervisor=bool(cpu.cr[0] & 0x80000000)) if cpu.cr[18] & 1 else pointer
        except (EmulationStop, ValueError):
            return 0
        if physical not in cpu.bus.i2c.bases:
            return 0
        device = cpu.bus.i2c.controllers[cpu.bus.i2c.bases.index(physical)]
        if type(device) is not I2C or device.control & 10 == 8:
            return 0
        loops = min(count // 8, cpu.regs[cnt_reg] - 1, 25000)
        if not loops:
            return 0
        cpu.regs[cnt_reg] -= loops
        cpu.c = 1
        cpu.steps += loops * 8
        cpu.bus.ticks += loops * 8
        cpu.bus.activity += loops
        event = {'operation': 'read', 'address': f'0x{physical:08x}', 'size': 4, 'value': None}
        for _ in range(min(loops, cpu.bus.events.maxlen)):
            cpu.bus.events.append(dict(event))
        instructions = [(address, word, decode(word, address), None) for address, word in zip(positions, words)]
        for _ in range(min(loops, (history.maxlen or 32) // 8 + 1)):
            history.extend(instructions)
        cpu.last_instruction = instructions[-1][:3]
        return loops * 8
