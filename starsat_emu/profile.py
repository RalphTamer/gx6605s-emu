"""Explicit, serializable assumptions for software-only board bring-up."""
from dataclasses import dataclass, field
import json
from pathlib import Path


@dataclass
class MachineProfile:
    name: str = "strict"
    description: str = "No unverified device reset values."
    assumptions: list = field(default_factory=list)
    clock_reset: dict = field(default_factory=dict)
    dram_controller: bool = False
    spi_flash: bool = False
    spi_writable: bool = False
    rtc: bool = False
    interrupt_controller: bool = False
    infrared: bool = False
    otp: bool = False
    video_registers: bool = False
    otp_info: object = None
    provisional_registers: dict = field(default_factory=dict)
    gpio_inputs: object = None
    video_reset: dict = field(default_factory=dict)
    timers: bool = False
    timer_input_hz: int = 27000000
    spi_jedec: str = "ef4016"
    i2c: bool = False
    ehci: bool = False
    ohci: bool = False
    timer_interrupts: bool = False
    instruction_hz: int = 100000
    video_timing: object = None
    spi_security: dict = field(default_factory=dict)
    graphics_accelerator: bool = False
    spi_unique_id: object = None
    otp_data: object = None
    av_microcontroller: object = None

    @classmethod
    def load(cls, value="strict"):
        if not isinstance(value, (str, Path)):
            raise ValueError("profile must be a name or file path")
        if value == "strict":
            return cls()
        builtins = {"exploratory": "gx6605s-exploratory.json", "receiver-observed": "gx6605s-receiver-observed.json", "receiver-runtime": "gx6605s-receiver-runtime.json"}
        path = Path(__file__).parent / "profiles" / builtins[value] if value in builtins else Path(value)
        data = json.loads(path.read_text())
        allowed = {"name", "description", "assumptions", "clock_reset", "dram_controller", "spi_flash", "spi_writable", "rtc", "interrupt_controller", "infrared", "otp", "video_registers", "otp_info", "provisional_registers", "gpio_inputs", "video_reset", "timers", "timer_input_hz", "spi_jedec", "i2c", "ehci", "ohci", "timer_interrupts", "instruction_hz", "video_timing", "spi_security", "graphics_accelerator", "spi_unique_id", "otp_data", "av_microcontroller"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("unknown profile fields")
        for key in ("name", "description"):
            if not isinstance(data.get(key), str):
                raise ValueError(f"profile {key} must be text")
        if not isinstance(data.get("assumptions", []), list) or any(not isinstance(v, str) for v in data.get("assumptions", [])):
            raise ValueError("profile assumptions must be a list of text")
        for key in ("dram_controller", "spi_flash", "spi_writable", "rtc", "interrupt_controller", "infrared", "otp", "video_registers", "timers", "i2c", "ehci", "ohci", "timer_interrupts", "graphics_accelerator"):
            if type(data.get(key, False)) is not bool:
                raise ValueError(f"profile {key} must be boolean")
        if data.get('spi_writable', False) and not data.get('spi_flash', False):
            raise ValueError('spi_writable requires a virtual SPI flash')
        if data.get('graphics_accelerator', False) and not data.get('interrupt_controller', False):
            raise ValueError('graphics accelerator requires the interrupt controller')
        if data.get('timer_interrupts', False) and not (data.get('timers', False) and data.get('interrupt_controller', False)):
            raise ValueError('timer interrupts require timers and the interrupt controller')
        rate = data.get('instruction_hz', 100000)
        if type(rate) is not int or not 1 <= rate <= 1000000000:
            raise ValueError('instruction_hz must be within 1..1 billion')
        timing = data.get('video_timing')
        if timing is not None and (not data.get('video_registers', False) or
                not isinstance(timing, dict) or not {'frame_hz', 'lines'} <= set(timing) or set(timing) - {'frame_hz', 'lines', 'interlaced', 'output_control'} or
                ('output_control' in timing and (type(timing['output_control']) is not int or not 0 <= timing['output_control'] <= 0xffffffff)) or
                type(timing.get('interlaced', False)) is not bool or
                type(timing.get('frame_hz')) is not int or not 1 <= timing['frame_hz'] <= 240 or
                type(timing.get('lines')) is not int or not 1 <= timing['lines'] <= 2048):
            raise ValueError('video_timing requires VPU storage, frame_hz=1..240 and lines=1..2048')
        frequency = data.get("timer_input_hz", 27000000)
        if type(frequency) is not int or not 1 <= frequency <= 1000000000:
            raise ValueError("timer_input_hz must be within 1..1 billion")
        jedec = data.get("spi_jedec", "ef4016")
        if not isinstance(jedec, str) or len(jedec) != 6 or any(c not in "0123456789abcdefABCDEF" for c in jedec):
            raise ValueError("spi_jedec must contain exactly three hex bytes")
        uid = data.get('spi_unique_id')
        if uid is not None and (not data.get('spi_flash', False) or not isinstance(uid,str) or len(uid)!=32 or any(c not in '0123456789abcdefABCDEF' for c in uid)):
            raise ValueError('spi_unique_id requires SPI and exactly 16 hex-encoded bytes')
        otp_data = data.get('otp_data')
        if otp_data is not None and (not data.get('otp', False) or not isinstance(otp_data, str) or len(otp_data) != 4096 or any(c not in '0123456789abcdefABCDEF' for c in otp_data)):
            raise ValueError('otp_data requires OTP and exactly 2048 hex-encoded bytes')
        av = data.get('av_microcontroller')
        if av is not None and (not isinstance(av, dict) or not {'boot_status'} <= set(av) <= {'boot_status', 'bootstrap', 'vpu_commands'} or type(av.get('boot_status')) is not int or not 0 <= av['boot_status'] <= 0xffffffff or av.get('bootstrap') not in (None, 'receiver-trace-20261114') or type(av.get('vpu_commands', False)) is not bool):
            raise ValueError('av_microcontroller requires a 32-bit boot_status and optional known bootstrap capture')
        for key in ("otp_info", "gpio_inputs"):
            if data.get(key) is not None and (type(data[key]) is not int or not 0 <= data[key] <= 0xffffffff):
                raise ValueError(f"{key} must be a 32-bit integer or null")
        registers = data.pop("clock_reset", {})
        provisional = data.pop("provisional_registers", {})
        video = data.pop("video_reset", {})
        security = data.pop('spi_security', {})
        if not isinstance(security, dict) or (security and not data.get('spi_flash', False)):
            raise ValueError('spi_security requires SPI and explicit bank-to-hex mappings')
        if not isinstance(video, dict):
            raise ValueError("video_reset must map physical register addresses to integers")
        if not isinstance(provisional, dict):
            raise ValueError("provisional_registers must map addresses to named reset values")
        if not isinstance(registers, dict):
            raise ValueError("clock_reset must map physical register addresses to integers")
        result = cls(**data)
        for bank, raw in security.items():
            try:
                address = int(bank, 0)
            except (TypeError, ValueError):
                raise ValueError('invalid security bank')
            if address not in (0x1000, 0x2000, 0x3000) or address in result.spi_security or not isinstance(raw, str) or len(raw) != 2048 or any(c not in '0123456789abcdefABCDEF' for c in raw):
                raise ValueError('security bank requires a unique valid address and exactly 1024 hex-encoded bytes')
            result.spi_security[address] = bytes.fromhex(raw)
        for address, raw in video.items():
            try:
                address = int(address, 0)
                value = int(raw, 0) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                raise ValueError("invalid video reset value")
            if type(value) is not int or not 0 <= value <= 0xffffffff or address % 4 or not 0x04800000 <= address < 0x04801000:
                raise ValueError("video reset requires an aligned VPU register and a 32-bit value")
            result.video_reset[address - 0x04800000] = value
        for address, entry in provisional.items():
            try:
                address = int(address, 0)
            except (TypeError, ValueError):
                raise ValueError("invalid provisional register address")
            if not isinstance(entry, dict) or not {"name", "reset"} <= set(entry) or set(entry) - {"name", "reset", "size", "readback", "write_clear", "fixed_bits"}:
                raise ValueError("provisional register requires name, reset and optional size/readback")
            size = entry.get("size", 4)
            if type(size) is not int or size not in (1, 2, 4) or not 0 <= address <= 0x100000000 - size or address % size:
                raise ValueError("provisional register requires a naturally aligned 1/2/4-byte address")
            reset = entry["reset"]
            if not isinstance(entry["name"], str) or (reset is not None and (type(reset) is not int or not 0 <= reset < 1 << (8 * size))):
                raise ValueError("invalid provisional register name/reset")
            fixed_bits = entry.get("fixed_bits", 0)
            if type(fixed_bits) is not int or not 0 <= fixed_bits < 1 << (8 * size):
                raise ValueError("invalid provisional register fixed_bits")
            if type(entry.get("readback", True)) is not bool or (not entry.get("readback", True) and reset is not None):
                raise ValueError("opaque writes require readback=false and reset=null")
            if type(entry.get('write_clear', False)) is not bool or (entry.get('write_clear', False) and (reset is None or not entry.get('readback', True))):
                raise ValueError('write-clear registers require an explicit initial value and readable state')
            if address in result.provisional_registers:
                raise ValueError("overlapping provisional registers")
            result.provisional_registers[address] = dict(entry)
        previous_end = -1
        for address in sorted(result.provisional_registers):
            if address < previous_end:
                raise ValueError("overlapping provisional registers")
            previous_end = address + result.provisional_registers[address].get("size", 4)
        from .memory import ClockControl
        for address, raw in registers.items():
            try:
                address = int(address, 0)
                value = int(raw, 0) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                raise ValueError("invalid clock reset value")
            if type(value) is not int or not 0 <= value <= 0xffffffff or address - ClockControl.base not in ClockControl.writable:
                raise ValueError("clock reset must select a supported clock register and a 32-bit value")
            result.clock_reset[address - ClockControl.base] = value
        return result

    def summary(self):
        import hashlib
        return {"name": self.name, "description": self.description, "assumptions": list(self.assumptions),
                "clock_reset": {hex(0x30a000 + k): hex(v) for k, v in self.clock_reset.items()},
                "dram_controller": self.dram_controller, "spi_flash": self.spi_flash, "spi_writable": self.spi_writable, "rtc": self.rtc,
                "interrupt_controller": self.interrupt_controller, "infrared": self.infrared, "otp": self.otp,
                "video_registers": self.video_registers, "otp_info": self.otp_info,
                "video_reset": {hex(0x04800000 + k): hex(v) for k, v in self.video_reset.items()},
                "provisional_registers": {hex(k): v for k, v in self.provisional_registers.items()},
                "gpio_inputs": self.gpio_inputs, "spi_jedec": self.spi_jedec,
                "timers": self.timers, "timer_input_hz": self.timer_input_hz, "i2c": self.i2c, "ehci": self.ehci, "ohci": self.ohci,
                "timer_interrupts": self.timer_interrupts, "instruction_hz": self.instruction_hz,
                "video_timing": self.video_timing, "graphics_accelerator": self.graphics_accelerator,
                "spi_unique_id": {'size': 16, 'sha256': hashlib.sha256(bytes.fromhex(self.spi_unique_id)).hexdigest()} if self.spi_unique_id is not None else None,
                "av_microcontroller": self.av_microcontroller,
                "otp_data": {'size': 2048, 'sha256': hashlib.sha256(bytes.fromhex(self.otp_data)).hexdigest()} if self.otp_data is not None else None,
                "spi_security": {hex(bank): {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                                 for bank, raw in self.spi_security.items()}}
