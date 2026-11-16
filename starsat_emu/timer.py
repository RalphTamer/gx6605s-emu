"""GX6605S dual 32-bit count-up timer with deterministic virtual time.

Register layout, reset/start, enable, overflow, and W1C status are described
by Linux's timer-gx6605s driver. Input clock/divider timing is a profile
assumption. IRQ pending is recorded; CPU interrupt delivery is not implemented.
"""
from .memory import EmulationStop


class Timers:
    base = 0x0020a000
    size = 0x80
    offsets = (0, 4, 0x10, 0x20, 0x24, 0x28)

    def __init__(self, clock, input_hz=27000000, instruction_hz=100000):
        self.clock, self.input_hz = clock, input_hz
        self.instruction_hz = instruction_hz
        self.channels = [{"status": 0, "count": 0, "control": 0, "config": 0,
                          "divider": 0, "initial": 0, "tick": clock(), "fraction": 0}
                         for _ in range(2)]

    def _sync(self, channel):
        now = self.clock()
        elapsed = max(0, now - channel["tick"])
        channel["tick"] = now
        if channel["control"] & 2 and channel["config"] & 1:
            # Shared instruction-based virtual clock; preserve fractional
            # timer input/divider progress between register accesses.
            numerator = channel["fraction"] + elapsed * self.input_hz
            denominator = self.instruction_hz * (channel["divider"] + 1)
            advance, channel["fraction"] = divmod(numerator, denominator)
            total = channel["count"] + advance
            if total > 0xffffffff:
                channel["status"] |= 1
                # 0x28 is TIM_RELOAD in the GX6605S register header.
                # The kernel's periodic ISR acknowledges without rearming;
                # Linux makes the event one-shot by writing reload=0.
                period = 0x100000000 - channel['initial']
                total = channel['initial'] + (total - 0x100000000) % period
            channel["count"] = total & 0xffffffff

    def access(self, offset, size, value=None):
        channel_index, local = divmod(offset, 0x40)
        if size != 4 or channel_index not in (0, 1) or local not in self.offsets:
            raise EmulationStop("unsupported-device-access", f"timer offset=0x{offset:x} size={size}")
        channel = self.channels[channel_index]
        self._sync(channel)
        key = {0: "status", 4: "count", 0x10: "control", 0x20: "config",
               0x24: "divider", 0x28: "initial"}[local]
        if value is None:
            return channel[key]
        value &= 0xffffffff
        if local == 4:
            raise EmulationStop("unsupported-device-access", "timer current value is read-only")
        if local == 0:
            if value & ~3:
                raise EmulationStop("unsupported-device-access", "unknown timer status bits")
            # Both receiver kernels acknowledge with 3. Only overflow bit 0
            # is generated here; clearing the unused low bit has no effect.
            channel["status"] &= ~(value & 1)
        elif local in (0x10, 0x20):
            if value & ~3:
                raise EmulationStop("unsupported-device-access", "unknown timer control/config bits")
            channel[key] = value
            if local == 0x10 and value & 1:
                channel["control"] &= ~2
                channel["count"] = channel["initial"]
                channel["fraction"] = 0
        else:
            channel[key] = value
            channel["fraction"] = 0
            if local == 0x28:
                channel["count"] = value

    def snapshot(self):
        for channel in self.channels:
            self._sync(channel)
        return {"input_hz": self.input_hz, "instruction_hz": self.instruction_hz,
                "virtual_microseconds_per_instruction": 1000000 / self.instruction_hz,
                "interrupt_delivery": "timer-0 delivery is selected by the board profile",
                "channels": [{k: v for k, v in c.items() if k not in ("tick", "fraction")} |
                             {"irq_pending": bool(c["status"] & 1 and c["config"] & 2)}
                             for c in self.channels]}

    def irq_pending(self, index=0):
        channel = self.channels[index]
        self._sync(channel)
        return bool(channel['status'] & 1 and channel['config'] & 2)

    def instructions_to_irq(self, index=0):
        channel = self.channels[index]
        self._sync(channel)
        if channel['status'] & 1 and channel['config'] & 2:
            return 0
        if channel['control'] & 2 and channel['config'] & 3 == 3:
            denominator = self.instruction_hz * (channel['divider'] + 1)
            needed = (0x100000000 - channel['count']) * denominator - channel['fraction']
            return max(1, (needed + self.input_hz - 1) // self.input_hz)
        return None
