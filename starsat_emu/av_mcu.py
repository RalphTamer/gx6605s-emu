"""AV upload tracking and an opt-in, physically captured bootstrap transition.

The transition is limited to the exact receiver-backup IRAM, configuration and
entire prepared AV DRAM window. It is a behavioral approximation, not an ISA
interpreter. Subsequent commands and all other programs stop explicitly.
"""
import hashlib
import struct
from .memory import EmulationStop


class AVMicrocontroller:
    base = 0x04200000
    size = 0x1100
    offsets = (0, 4, 0x0c, 0x18, 0x20, 0x58, 0x100, 0x104, 0x108, 0x10c, 0x110,
               0x114, *range(0x120, 0x140, 4), *range(0x150, 0x160, 4),
               0x160, 0x164, 0x168, 0x16c, 0x170, 0x174, *range(0x178, 0x1f4, 4),
               0x105c, 0x10f0, 0x10f4)
    word_count = 2048
    captured_bootstrap = 'receiver-trace-20261114'
    captured_iram_sha256 = 'cc07c07596921acc9ea494ffe45a43b0939d10df369b8efdb9c65ad0ab1b7863'
    captured_ram_sha256 = '9659eaa59e378b8099ba0a1c922f6d7973f49f45e6ab6eea31baf1ba738cd386'
    captured_configuration = {0: 1, 0x100: 0x12633000, 0x104: 0x12651000,
                              0x108: 0x12678000, 0x10c: 0, 0x110: 4,
                              0x170: 0, 0x160: 1, 0x58: 0x1e000}

    def __init__(self, boot_status, bootstrap=None, vpu_commands=False):
        if type(boot_status) is not int or not 0 <= boot_status <= 0xffffffff:
            raise ValueError('AV MCU boot status requires an explicit 32-bit observation')
        self.boot_status = boot_status
        if bootstrap not in (None, self.captured_bootstrap):
            raise ValueError('unknown AV bootstrap capture')
        if type(vpu_commands) is not bool:
            raise ValueError('vpu_commands must be boolean')
        self.bootstrap = bootstrap
        self.vpu_commands = vpu_commands
        self.bootstrap_completed = False
        self.iram = [None] * self.word_count
        self.registers = {}
        self.upload_writes = 0
        self.starts = 0
        self.started = False
        self.commands_issued = []

    def contains(self, address, size):
        return size == 4 and address - self.base in self.offsets

    def program_bytes(self):
        if any(word is None for word in self.iram):
            raise ValueError('AV MCU instruction memory is incomplete')
        return struct.pack('<%dH' % self.word_count, *self.iram)

    def _captured_startup(self, bus):
        # The physical experiment observed no changes anywhere in this complete
        # 0x90000-byte window. Validate every byte before allowing its measured
        # mailbox transition; the timing of this transition is not cycle-accurate.
        valid = (self.boot_status == 0 and self.starts == 1 and
                 self.registers == self.captured_configuration and
                 hashlib.sha256(self.program_bytes()).hexdigest() == self.captured_iram_sha256)
        region = next((r for r in bus.regions if r.base <= 0x12633000 and
                       r.base + len(r.data) >= 0x126c3000), None) if bus is not None else None
        if region is None:
            valid = False
        elif hashlib.sha256(region.data[0x12633000-region.base:0x126c3000-region.base]).hexdigest() != self.captured_ram_sha256:
            valid = False
        if not valid:
            raise EmulationStop('unsupported-av-bootstrap',
                'AV startup differs from the physical receiver capture (IRAM, configuration or full AV RAM); no completion supplied')
        self.bootstrap_completed = True
        self.registers.update({0x20: 0, 0x160: 0, 0x164: 0, 0x168: 0, 0x174: 0})

    def access(self, offset, size, value=None, bus=None):
        if size != 4 or offset not in self.offsets:
            raise EmulationStop('unsupported-device-access', 'AV MCU requires an observed aligned word register')
        if value is None:
            if offset == 0x18 and not self.starts:
                return self.boot_status
            if (offset == 0x160 and self.started and self.bootstrap == self.captured_bootstrap
                    and not self.bootstrap_completed):
                self._captured_startup(bus)
            if self.bootstrap_completed:
                if self.vpu_commands:
                    if offset == 0x18:
                        return 0x8b
                    if offset == 0x10f4:
                        return self.registers.get(0x10f4, 0x77 if self.registers.get(0x10f0) == 0x11 else 0)
                    return self.registers.get(offset, 0)
                if offset in (0x20, 0x160, 0x164, 0x168, 0x170, 0x174):
                    return self.registers[offset]
                if offset == 0x18:
                    # Representative idle status measured after stopping the
                    # bootstrap. Exposing it while idle is an explicit functional
                    # approximation; no running instruction-PC timing is claimed.
                    return 0x8b
            if offset == 0x160 and self.started:
                digest = hashlib.sha256(self.program_bytes()).hexdigest()
                raise EmulationStop('unsupported-av-microcode',
                    'AV MCU startup is waiting for execution of its 2048-word program; '
                    'instruction set/backend not implemented (SHA256 %s)' % digest)
            raise EmulationStop('unmodeled-device-readback',
                                'AV MCU register 0x%08x has no modeled readback' % (self.base + offset))
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise EmulationStop('unsupported-device-access', 'AV MCU write must contain one 32-bit word')
        if self.bootstrap_completed and not (offset == 0 and value == 0):
            if self.vpu_commands:
                self.registers[offset] = value
                if offset == 0x0c and value & 1:
                    self.registers[0x174] = 0
                elif offset == 0x164:
                    self.commands_issued.append(value)
                    self.registers[0x160] = 0
                    if value == 0x0f:  # FIRMWARE_GET
                        self.registers[0x1c0] = 0x00013100
                    elif value == 0x01:  # DEC_SEQ_INIT
                        self.registers[0x174] = self.registers.get(0x174, 0) | 0x1
                        self.registers[0x1c0] = 1
                    elif value == 0x02:  # DEC_SEQ_END
                        self.registers[0x174] = self.registers.get(0x174, 0) | 0x2
                        self.registers[0x1c0] = 1
                elif offset == 0x10f0 and value == 0x11:
                    self.registers[0x10f4] = 0x77
                return None
            raise EmulationStop('unsupported-av-command',
                'AV bootstrap capture ends at idle; further host commands require a functional MCU backend')
        if offset == 0x18:
            raise EmulationStop('unsupported-device-access', 'AV MCU boot status writes are unimplemented')
        if offset == 4:
            index = value >> 16
            if index >= self.word_count or self.started:
                raise EmulationStop('unsupported-av-upload', 'AV MCU instruction index is outside stopped 2048-word IRAM')
            self.iram[index] = value & 0xffff
            self.upload_writes += 1
            return None
        if offset == 0:
            if value not in (0, 1):
                raise EmulationStop('unsupported-av-command', 'AV MCU control supports only observed stop/start values')
            if value == 1:
                if any(word is None for word in self.iram):
                    raise EmulationStop('missing-av-microcode', 'AV MCU start requires a complete captured instruction upload')
                self.starts += 1
            self.started = bool(value)
        self.registers[offset] = value

    def snapshot(self):
        loaded = sum(word is not None for word in self.iram)
        return {'status': ('captured-bootstrap-idle' if self.bootstrap_completed else 'uploaded-awaiting-implementation') if self.started else 'stopped',
                'iram_words': self.word_count, 'loaded_words': loaded,
                'upload_writes': self.upload_writes, 'starts': self.starts,
                'program_sha256': hashlib.sha256(self.program_bytes()).hexdigest() if loaded == self.word_count else None,
                'boot_status_observation': hex(self.boot_status),
                'bootstrap_model': self.bootstrap,
                'bootstrap_completed': self.bootstrap_completed,
                'host_registers_written': {hex(offset): hex(value) for offset, value in self.registers.items()},
                'limitations': 'Only the opt-in exact receiver bootstrap transition is captured. The stopped +18=8B observation also represents idle running status as an approximation. No ISA execution, subsequent commands, DMA, interrupts or physical timing model.'}
