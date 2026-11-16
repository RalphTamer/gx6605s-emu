"""Read-only loader for the update layout observed in this workspace."""
from pathlib import Path
import hashlib
import struct
import zlib

from .memory import Bus, DRAMController, InterruptMasks, Infrared, VideoRegisters
from .cpu import CPU


class Firmware:
    def __init__(self, path, offset=None):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if offset is None:
            magic = bytes.fromhex("aa55aa55")
            offset = next((p for p in (0, 0x100) if self.data[p:p + 4] == magic), -1)
        if offset < 0 or len(self.data) < offset + 8:
            raise ValueError("image is too small for the selected payload offset")
        self.offset = offset
        self.payload = self.data[offset:]
        if self.payload[:4] != bytes.fromhex("aa55aa55"):
            raise ValueError("selected payload does not start with GX boot magic aa55aa55")

    def inspect(self):
        filesystems = []
        pos = 0
        while True:
            pos = self.data.find(bytes.fromhex("453dcd28"), pos)
            if pos < 0:
                break
            if pos + 76 <= len(self.data):
                size = struct.unpack_from("<I", self.data, pos + 4)[0]
                if size >= 76 and pos + size <= len(self.data) and self.data[pos + 16:pos + 32] == b"Compressed ROMFS":
                    fs = bytearray(self.data[pos:pos + size])
                    expected = struct.unpack_from("<I", fs, 32)[0]
                    fs[32:36] = bytes(4)
                    filesystems.append({"offset": hex(pos), "size": size,
                                        "stored_crc32": f"{expected:08x}",
                                        "crc_valid": zlib.crc32(fs) == expected})
            pos += 4
        return {"file": str(self.path), "size": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(),
                "payload_offset": hex(self.offset), "payload_size": len(self.payload),
                "boot_magic": self.payload[:4].hex(), "cramfs": filesystems}

    def machine(self, base=0x100000, load_size=0x3000, entry=None, profile=None):
        if not 8 <= load_size <= min(0x10000, len(self.payload) - 4):
            raise ValueError("load size must fit the payload and the 64 KiB boot SRAM window")
        bus = Bus()
        # The magic prefixes the code; absolute literal pointers align only
        # when file offset 0x104, rather than the magic at 0x100, maps to base.
        bus.map("boot-sram", base, 0x10000, self.payload[4:4 + load_size])
        bus.map("dram", 0x10000000, 64 * 1024 * 1024)
        if profile is not None:
            bus.timer_interrupts = profile.timer_interrupts
            bus.clocks.registers.update(profile.clock_reset)
            if profile.dram_controller:
                bus.dram_controller = DRAMController()
            if profile.spi_flash:
                from .spi import SPIController, SerialNOR
                bus.spi = SPIController(SerialNOR(self.payload, writable=profile.spi_writable))
                bus.spi.flash.jedec = bytes.fromhex(profile.spi_jedec)
                bus.spi.flash.security.update(profile.spi_security)
                bus.spi.flash.unique_id = bytes.fromhex(profile.spi_unique_id) if profile.spi_unique_id is not None else None
            if profile.rtc:
                from .rtc import RTC
                import weakref
                clock_bus = weakref.ref(bus)
                bus.rtc = RTC(lambda: clock_bus().ticks, profile.instruction_hz)
            if profile.timers:
                from .timer import Timers
                import weakref
                timer_bus = weakref.ref(bus)
                bus.timers = Timers(lambda: timer_bus().ticks, profile.timer_input_hz, profile.instruction_hz)
            if profile.i2c:
                from .i2c import I2CControllers
                bus.i2c = I2CControllers()
            if profile.ehci:
                from .ehci import EHCI
                import weakref
                ehci_bus = weakref.ref(bus)
                bus.ehci = EHCI(lambda: ehci_bus().ticks, profile.instruction_hz)
            if profile.ohci:
                from .ohci import OHCIControllers
                import weakref
                ohci_bus = weakref.ref(bus)
                bus.ohci = OHCIControllers(lambda: ohci_bus().ticks, profile.instruction_hz)
            if profile.interrupt_controller:
                bus.interrupt_masks = InterruptMasks(full=True)
            if profile.graphics_accelerator:
                from .graphics import GraphicsAccelerator
                bus.graphics = GraphicsAccelerator(bus)
            if profile.av_microcontroller is not None:
                from .av_mcu import AVMicrocontroller
                bus.av_mcu = AVMicrocontroller(**profile.av_microcontroller)
            if profile.infrared:
                bus.infrared = Infrared()
            if profile.otp:
                from .otp import OTP
                bus.otp = OTP(profile.otp_info, bytes.fromhex(profile.otp_data) if profile.otp_data is not None else None)
            if profile.video_registers:
                import weakref
                video_bus = weakref.ref(bus)
                bus.video = VideoRegisters(lambda: video_bus().ticks, profile.instruction_hz, profile.video_timing)
                bus.video.registers.update(profile.video_reset)
                if profile.video_timing is not None and 'output_control' in profile.video_timing:
                    from .memory import VideoOutput
                    bus.video_output = VideoOutput(bus.video, profile.video_timing['output_control'])
            if profile.gpio_inputs is not None:
                from .gpio import GPIO
                bus.gpio = GPIO(profile.gpio_inputs)
            for address, register in profile.provisional_registers.items():
                size = register.get("size", 4)
                devices = (bus.uart, bus.interrupt_masks, bus.clocks, bus.dram_controller, bus.spi, bus.timers, bus.i2c, bus.ehci, bus.ohci,
                           bus.rtc, bus.infrared, bus.otp, bus.video, bus.video_output, bus.graphics, bus.av_mcu, bus.gpio)
                if any(region.base < address + size and address < region.base + len(region.data) for region in bus.regions) or any(
                    device is not None and device.base < address + size and address < device.base + device.size and (
                        not hasattr(device, "contains") or device.contains(address, size)
                    ) for device in devices
                ):
                    raise ValueError("provisional register overlaps modeled memory/device")
                bus.provisional[address] = {"name": register["name"], "value": register["reset"], "size": size}
                if not register.get("readback", True):
                    bus.provisional[address]["readback"] = False
                if register.get('write_clear', False):
                    bus.provisional[address]['write_clear'] = True
                if register.get('fixed_bits', 0):
                    bus.provisional[address]['fixed_bits'] = register['fixed_bits']
        cpu = CPU(bus, base if entry is None else entry)
        return cpu
