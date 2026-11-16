"""Execution session shared by command-line and desktop clients."""
from collections import deque
from .memory import EmulationStop
from .video import OSDRenderer
from .accelerator import Accelerator


class Session:
    def __init__(self, firmware, profile, base=0x100000, load_size=0x3000, entry=None):
        self.firmware = firmware
        self.profile = profile
        self.cpu = firmware.machine(base, load_size, entry, profile)
        self.layout = {"boot_sram_base": hex(base), "code_file_offset": hex(firmware.offset + 4),
                       "preload_bytes": load_size, "dram_base": "0x10000000", "dram_size": 0x4000000,
                       "assumptions": ["BootROM replaced with a provisional SRAM preload", "zero-filled initial registers/RAM",
                                       "supervisor mode at entry", "cache timing omitted", "MSA reset values follow C-SKY QEMU"]}
        self.firmware_info = firmware.inspect()
        self.history = deque(maxlen=32)
        self.stop = None
        self.video_renderer = OSDRenderer()
        self.accelerator = Accelerator(self.cpu)
        self._poll_seen = 0
        self._idle_signature = None
        self._idle_repeats = 0

    def advance(self, count, breakpoints=(), trace=None):
        self.stop = None
        remaining = count
        while remaining:
            if not trace:
                done = self.accelerator.advance(self.cpu, remaining, self.history, breakpoints)
                remaining -= done
                if not remaining:
                    break
            if self.cpu.pc in breakpoints:
                self.stop = {"reason": "breakpoint", "detail": hex(self.cpu.pc)}
                break
            before = self.cpu.snapshot() if trace else None
            try:
                self.cpu.step()
            except EmulationStop as error:
                self.history.append((*self.cpu.last_instruction, error.kind))
                self.stop = {"reason": error.kind, "detail": error.detail}
                if trace:
                    trace(self._row(self.history[-1], before))
                break
            self.history.append((*self.cpu.last_instruction, None))
            if trace:
                trace(self._row(self.history[-1], before))
            remaining -= 1
            bus = self.cpu.bus
            if bus.uart.empty_polls != self._poll_seen:
                self._poll_seen = bus.uart.empty_polls
                signature = (self.cpu.pc, tuple(self.cpu.regs), self.cpu.c,
                             tuple(self.cpu.cr), bus.activity)
                self._idle_repeats = self._idle_repeats + 1 if signature == self._idle_signature else 0
                self._idle_signature = signature
                if self._idle_repeats >= 64 and not bus.uart.rx:
                    self.stop = {"reason": "waiting-for-uart", "detail": "stable polling loop; send serial input to resume"}
                    break
        return self.stop

    def _row(self, item, before=None):
        pc, word, ins, error = item
        row = {"pc": f"0x{pc:08x}", "word": f"0x{word:04x}" if word is not None else None,
               "instruction": str(ins) if ins is not None else "instruction fetch", "status": "stopped" if error else "executed"}
        if error:
            row["reason"] = error
        if before is not None:
            row.update({"step": before["steps"], "next_pc": hex(self.cpu.pc), "carry": self.cpu.c,
                        "changed_registers": {f"r{i}": hex(value) for i, value in enumerate(self.cpu.regs)
                                              if f"0x{value:08x}" != before["registers"][i]}})
        return row

    def memory(self, address, size):
        if not 0 < size <= 4096 or not 0 <= address <= 0xffffffff or address + size > 0x100000000:
            raise ValueError("memory inspection requires a 32-bit address and 1..4096 bytes")
        result = bytearray()
        for virtual in range(address, address + size):
            physical = self.cpu.mmu.translate(virtual) if self.cpu.cr[18] & 1 else virtual
            region = next((r for r in self.cpu.bus.regions if r.base <= physical < r.base + len(r.data)), None)
            if region is None:
                raise ValueError("debugger memory inspection only reads RAM, avoiding peripheral side effects")
            result.append(region.data[physical - region.base])
        return bytes(result)

    def snapshot(self, include_frame=False):
        c, bus = self.cpu, self.cpu.bus
        return {"status": "experimental-partial-emulation", **(self.stop or {"reason": "paused", "detail": ""}),
                "firmware": self.firmware_info, "profile": self.profile.summary(), "machine": self.layout,
                "cpu": c.snapshot(), "mmu": c.mmu.snapshot(),
                "execution_backend": "native RAM interpreter + Python device/control fallback" if self.accelerator.native else "Python interpreter",
                "recent_instructions": [self._row(row) for row in self.history],
                "interrupt_masks": {hex(k): hex(v) for k, v in bus.interrupt_masks.registers.items()},
                "clock_registers_written": {hex(k): hex(v) for k, v in bus.clocks.registers.items()},
                "spi": bus.spi.snapshot() if bus.spi else None,
                "dram_initialized": bool(bus.dram_controller and bus.dram_controller.started),
                "timers": bus.timers.snapshot() if bus.timers else None,
                "i2c": bus.i2c.snapshot() if bus.i2c else None,
                "ehci": bus.ehci.snapshot() if bus.ehci else None,
                "ohci": bus.ohci.snapshot() if bus.ohci else None,
                "graphics": bus.graphics.snapshot() if bus.graphics else None,
                "av_microcontroller": bus.av_mcu.snapshot() if getattr(bus, 'av_mcu', None) else None,
                "otp": {"source": "captured" if getattr(bus.otp, 'captured', False) else "synthetic-zero",
                        "read_operations": bus.otp.reads} if bus.otp else None,
                "video_registers": {hex(k): hex(v) for k, v in bus.video.registers.items()} if bus.video else None,
                "video_timing": bus.video.timing_snapshot() if bus.video else None,
                "video_assumed_zero_reads": [hex(v) for v in sorted(bus.video.zero_reads)] if bus.video else [],
                "provisional_registers_used": {hex(k): bus.provisional[k] for k in sorted(bus.provisional_used)},
                "gpio": bus.gpio.snapshot() if bus.gpio else None,
                "display": self.video_renderer.capture(bus, include_frame),
                "device_accesses": list(bus.events), "uart_output_hex": bus.uart.tx.hex(),
                "uart_output_text": bus.uart.tx.decode("utf8", errors="replace")}
