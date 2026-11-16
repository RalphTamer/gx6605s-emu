"""Strict physical address bus. Unimplemented devices never return invented data."""
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import json


class EmulationStop(Exception):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class Region:
    name: str
    base: int
    data: bytearray
    readonly: bool = False


class UART:
    """GX6605S polling UART, no timing or interrupts. See docs/references.md."""
    base = 0x00400000
    size = 0x18

    def __init__(self):
        self.registers = {0: 0, 0x0c: 0, 0x10: 0}
        self.rx = deque()
        self.tx = bytearray()
        self.empty_polls = 0

    def access(self, offset, size, value=None):
        if offset not in (0, 4, 8, 12, 16, 20) or size not in (1, 4):
            raise EmulationStop("unsupported-device-access", "UART requires aligned byte or word accesses")
        if value is None:
            if offset == 4:
                if not self.rx:
                    self.empty_polls += 1
                return 0x80 | (0x20 if self.rx else 0)
            if offset == 8:
                return self.rx.popleft() if self.rx else 0
            if offset not in self.registers:
                raise EmulationStop("unknown-device-reset", f"UART configuration 0x{self.base + offset:08x} has not been written")
            return self.registers[offset] & ((1 << (8 * size)) - 1)
        if offset == 8:
            self.tx.append(value & 0xff)
        elif offset == 4:
            raise EmulationStop("unsupported-device-access", "UART status writes are not implemented")
        else:
            self.registers[offset] = value


class InterruptMasks:
    """IRQ/FIQ gates and masks; level inputs are supplied by modeled devices."""
    base = 0x00500000
    size = 0x200

    def __init__(self, full=False):
        self.full = full
        self.sources = 0
        self._route_cache = {}
        self.registers = {0x50: 0, 0x54: 0, 0x150: 0, 0x154: 0}
        if full:
            self.registers.update({base + offset: 0 for base in (0, 0x100)
                                   for offset in (0, 4, 0x10, 0x14, 0x40, 0x44, *range(0x60, 0xa0, 4))})

    def access(self, offset, size, value=None):
        if size != 4:
            raise EmulationStop("unsupported-device-access", f"interrupt controller offset=0x{offset:x} size={size}")
        if self.full and (offset & 0xff) in (0x20, 0x24, 0x30, 0x34):
            target = (offset & 0x100) + 0x40 + (offset & 4)
            if value is not None:
                self.registers[target] = self.registers[target] | value if offset & 0xf0 == 0x20 else self.registers[target] & ~value
            return self.registers[target]
        if offset not in self.registers:
            raise EmulationStop("unsupported-device-access", f"interrupt controller offset=0x{offset:x} size={size}")
        if value is None:
            if self.full and (offset & 0xff) in (0, 4, 0x10, 0x14):
                bank = offset & 0x100
                shift = 32 if offset & 4 else 0
                routed = self.routed_sources(bank)
                pending = ((routed >> shift) & 0xffffffff) & self.registers[bank + 0x40 + (offset & 4)]
                if offset & 0x10 == 0:
                    pending &= ~self.registers[bank + 0x50 + (offset & 4)]
                return pending
            return self.registers[offset]
        if self.full and (offset & 0xff) in (0, 4, 0x10, 0x14):
            raise EmulationStop("unsupported-device-access", "interrupt status writes are not implemented")
        self.registers[offset] = value & 0xffffffff
        if 0x60 <= (offset & 0xff) < 0xa0:
            if not hasattr(self, '_route_cache'):self._route_cache = {}
            self._route_cache.pop(offset & 0x100, None)

    def routed_sources(self, bank=0, sources=None):
        """Each output's source selector is the low six bits of its byte."""
        sources = self.sources if sources is None else sources
        if not sources:return 0
        if not hasattr(self, '_route_cache'):self._route_cache = {}
        if bank not in self._route_cache:
            masks = [0] * 64
            for index in range(64):
                selector = self.registers.get(bank + 0x60 + (index // 4) * 4, 0)
                source = (selector >> ((index % 4) * 8)) & 63
                masks[source] |= 1 << index
            self._route_cache[bank] = masks
        outputs = 0
        while sources:
            bit = sources & -sources
            outputs |= self._route_cache[bank][bit.bit_length() - 1]
            sources ^= bit
        return outputs


class ClockControl:
    """Configuration readback only; no PLL timing or inferred reset values.

    Register addresses and the write/readback sequence come from xboot's
    GX6605S clock driver. Reads of registers the guest has not written stop.
    """
    base = 0x0030a000
    size = 0x1000
    writable = frozenset(json.loads(Path(__file__).with_name("gctl_registers.json").read_text())["registers"].values())

    def __init__(self):
        self.registers = {}

    def access(self, offset, size, value=None):
        if size != 4 or offset not in self.writable:
            raise EmulationStop("unsupported-device-access", f"clock controller offset=0x{offset:x} size={size}")
        if value is None:
            if offset not in self.registers:
                raise EmulationStop("unknown-device-reset", f"clock register 0x{self.base + offset:08x} has no verified reset value")
            return self.registers[offset]
        # The chip has DATA/SET/CLEAR aliases for reset and clock gating banks.
        aliases = {4: (0, True), 8: (0, False), 0x10: (0xc, True), 0x14: (0xc, False),
                   0x1c: (0x18, True), 0x20: (0x18, False), 0x6c: (0x68, True),
                   0x70: (0x68, False), 0x1c4: (0x1c0, True), 0x1c8: (0x1c0, False)}
        if offset in aliases:
            target, setting = aliases[offset]
            if target not in self.registers:
                raise EmulationStop("unknown-device-reset", f"clock register 0x{self.base + target:08x} has no verified reset value")
            self.registers[target] = self.registers[target] | value if setting else self.registers[target] & ~value
            return
        self.registers[offset] = value & 0xffffffff


class DRAMController:
    """Functional Denali configuration/readiness, enabled by an explicit profile."""
    base = 0x00c00000
    size = 0x1000

    def __init__(self):
        self.registers = {}
        self.started = False

    def access(self, offset, size, value=None):
        if size != 4 or offset % 4 or not (0 <= offset < 155 * 4 or 0x400 <= offset < 0x400 + 26 * 4):
            raise EmulationStop("unsupported-device-access", f"DRAM controller offset=0x{offset:x} size={size}")
        if value is None:
            if offset not in self.registers:
                raise EmulationStop("unknown-device-reset", f"DRAM register 0x{self.base + offset:08x} has not been initialized")
            result = self.registers[offset]
            if offset == 0xb8 and self.started:
                result |= 0x20
            return result
        self.registers[offset] = value & 0xffffffff
        if offset == 0:
            self.started = bool(value & 1)


class Infrared:
    """GX6605S CIR receiver (0x00204000, IRQ 4) with NEC frame injection."""
    base = 0x00204000
    size = 0x10
    irq = 4

    def __init__(self):
        self.registers = {0: 0, 12: 0}
        self.fifo = deque()
        self.pending = False

    def queue_nec(self, code32):
        code32 &= 0xffffffff
        # Leader pulse (9000us low, 13500us total) followed by 32 MSB-first bits.
        # Guest NEC decoder (0x903ad050: cmphs 1685, hi16) treats <=1685 as 1, >1685 as 0.
        self.fifo.append((13500 << 16) | 9000)
        for shift in range(31, -1, -1):
            total = 1120 if (code32 >> shift) & 1 else 2250
            self.fifo.append((total << 16) | 560)
        self.pending = True

    def irq_pending(self):
        return self.pending and bool(self.registers[0] & 8)

    def access(self, offset, size, value=None):
        if size != 4 or offset not in (0, 4, 8, 12):
            raise EmulationStop("unsupported-device-access", f"CIR offset=0x{offset:x} size={size}")
        if value is None:
            if offset == 4:
                return (min(len(self.fifo), 63) << 3) | (2 if self.pending else 0)
            if offset == 8:
                if not self.fifo:
                    raise EmulationStop("empty-device-fifo", "CIR FIFO read with no remote input")
                return self.fifo.popleft()
            return self.registers[offset]
        if offset == 8:
            raise EmulationStop("unsupported-device-access", "CIR receive FIFO is read-only")
        if offset == 4:
            if value & 2:
                self.pending = bool(self.fifo)
        else:
            self.registers[offset] = value & 0xffffffff


class VideoRegisters:
    """VPU configuration storage with optional explicitly configured raster timing."""
    base = 0x04800000
    size = 0x1000

    def __init__(self, clock=None, instruction_hz=100000, timing=None):
        self.registers = {}
        self.zero_reads = set()
        self.clock, self.instruction_hz, self.timing = clock, instruction_hz, timing
        self.scan_start = clock() if clock is not None else 0

    def access(self, offset, size, value=None):
        if size != 4 or offset % 4:
            raise EmulationStop("unsupported-device-access", f"VPU offset=0x{offset:x} size={size}")
        if offset == 0xe4 and self.timing is not None:
            if value is not None:
                raise EmulationStop('readonly-write', 'VPU scan-line counter is read-only')
            elapsed = max(0, self.clock() - self.scan_start)
            if self.timing.get('interlaced', False):
                halves = (elapsed * self.timing['frame_hz'] * self.timing['lines'] * 2 // self.instruction_hz) % (self.timing['lines'] * 2)
                return ((halves % self.timing['lines']) // 2) | (0x800 if halves >= self.timing['lines'] else 0)
            total = elapsed * self.timing['frame_hz'] * self.timing['lines'] // self.instruction_hz
            # The kernel's timing calibration waits for bit 11 transitions in
            # progressive modes too. Model it as frame parity there; physical
            # samples directly establish its field use only in interlaced mode.
            return total % self.timing['lines'] | (0x800 if (total // self.timing['lines']) & 1 else 0)
        if value is None:
            if offset not in self.registers:
                if 0x140 <= offset < 0x1c0:
                    raise EmulationStop("unknown-device-reset",
                        f"VPU register 0x{self.base + offset:08x} is used by the payload decoder; supply verified video_reset values in a machine profile")
                self.zero_reads.add(offset)
            return self.registers.get(offset, 0)
        self.registers[offset] = value & 0xffffffff

    def timing_snapshot(self):
        if self.timing is None:
            return {'enabled': False}
        return {'enabled': True, **self.timing, 'scan_line': self.access(0xe4, 4),
                'mode': 'functional raster; vertical-blank IRQs are not modeled'}


class VideoOutput:
    """VOUT mode selection, using the public GX6605S driver's format table.

    Only the control word is modeled. DAC/HDMI signals and mode-change IRQs
    are not generated. Nominal 60 Hz modes use 60 rather than 60000/1001 Hz.
    """
    base = 0x04804000
    size = 4
    # TV_FORMAT -> (total lines, frame rate, interlaced). Bit 5 selects 50 Hz
    # for HD modes; the SD modes encode the television standard in the format.
    modes = {0: (625, 25, True), 4: (525, 30, True),
             8: (525, 60, False), 9: (625, 50, False)}

    def __init__(self, video, control):
        self.video = video
        self.control = None
        self.access(0, 4, control)

    def access(self, offset, size, value=None):
        if offset != 0 or size != 4:
            raise EmulationStop('unsupported-device-access', 'VOUT control requires a word access')
        if value is None:
            return self.control
        value &= 0xffffffff
        fmt = value & 31
        if fmt in self.modes:
            lines, rate, interlaced = self.modes[fmt]
        elif fmt in (6, 13, 14):
            lines = 750 if fmt == 13 else 1125
            interlaced = fmt == 6
            rate = (25 if interlaced else 50) if value & 32 else (30 if interlaced else 60)
        else:
            raise EmulationStop('unsupported-video-mode', 'VOUT TV_FORMAT=%d is not modeled' % fmt)
        if self.control is None or (self.control ^ value) & 63:
            self.video.timing = {'frame_hz': rate, 'lines': lines, 'interlaced': interlaced}
            self.video.scan_start = self.video.clock()
        self.control = value


class Bus:
    def __init__(self):
        self.regions = []
        self.uart = UART()
        self.interrupt_masks = InterruptMasks()
        self.clocks = ClockControl()
        self.dram_controller = None
        self.spi = None
        self.rtc = None
        self.timers = None
        self.i2c = None
        self.ehci = None
        self.ohci = None
        self.timer_interrupts = False
        self.infrared = None
        self.otp = None
        self.video = None
        self.video_output = None
        self.graphics = None
        self.av_mcu = None
        self.gpio = None
        self.ticks = 0
        self.provisional = {}
        self.provisional_used = set()
        self.events = deque(maxlen=64)
        self.activity = 0

    def map(self, name, base, size, initial=b"", readonly=False):
        if base < 0 or size <= 0 or base + size > 0x100000000 or len(initial) > size:
            raise ValueError("invalid memory region")
        for region in self.regions:
            if base < region.base + len(region.data) and region.base < base + size:
                raise ValueError("overlapping memory regions")
        region = Region(name, base, bytearray(initial) + bytearray(size - len(initial)), readonly)
        self.regions.append(region)
        return region

    def access(self, address, size, value=None, fetch=False):
        if size not in (1, 2, 4) or not 0 <= address <= 0xffffffff or address + size > 0x100000000:
            raise EmulationStop("invalid-memory-access", f"address=0x{address:x} size={size}")
        if address % size:
            raise EmulationStop("misaligned-access", f"address=0x{address:08x} size={size}")
        for region in self.regions:
            offset = address - region.base
            if 0 <= offset and offset + size <= len(region.data):
                if value is None:
                    return int.from_bytes(region.data[offset:offset + size], "little")
                if region.readonly:
                    raise EmulationStop("readonly-write", f"{region.name} at 0x{address:08x}")
                self.activity += 1
                region.data[offset:offset + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")
                return None
        event = {"operation": "fetch" if fetch else "read" if value is None else "write",
                 "address": f"0x{address:08x}", "size": size, "value": value}
        self.events.append(event)
        if fetch or value is not None or address != self.uart.base + 4:
            self.activity += 1
        if not fetch and self.uart.base <= address and address + size <= self.uart.base + self.uart.size:
            return self.uart.access(address - self.uart.base, size, value)
        if not fetch and self.interrupt_masks.base <= address and address + size <= self.interrupt_masks.base + self.interrupt_masks.size:
            return self.interrupt_masks.access(address - self.interrupt_masks.base, size, value)
        if not fetch and self.clocks.base <= address and address + size <= self.clocks.base + self.clocks.size:
            return self.clocks.access(address - self.clocks.base, size, value)
        for device in (self.dram_controller, self.spi, self.rtc, self.timers, self.i2c, self.ehci, self.ohci, self.infrared, self.otp, self.video, self.video_output, self.graphics, getattr(self, 'av_mcu', None), self.gpio):
            if device is not None and not fetch and device.base <= address and address + size <= device.base + device.size and (
                not hasattr(device, "contains") or device.contains(address, size)
            ):
                if device is getattr(self, 'av_mcu', None):
                    return device.access(address - device.base, size, value, bus=self)
                return device.access(address - device.base, size, value)
        if not fetch and address in self.provisional:
            register = self.provisional[address]
            if size != register.get("size", 4):
                raise EmulationStop("unsupported-device-access", "provisional register access width differs from its explicit profile entry")
            self.provisional_used.add(address)
            if value is None:
                if not register.get("readback", True):
                    raise EmulationStop("unmodeled-device-readback", "opaque register 0x%08x has unmodeled write side effects; stored writes cannot supply readback" % address)
                if register["value"] is None:
                    raise EmulationStop("unknown-device-reset", "provisional register 0x%08x has not been initialized by the guest" % address)
                return register["value"] | register.get("fixed_bits", 0)
            bits = value & ((1 << (8 * size)) - 1)
            register["value"] = register['value'] & ~bits if register.get('write_clear', False) else bits
            return None
        raise EmulationStop("unmapped-access", str(event))

    def read(self, address, size=4, fetch=False):
        return self.access(address, size, fetch=fetch)

    def write(self, address, value, size=4):
        self.access(address, size, value=value)
