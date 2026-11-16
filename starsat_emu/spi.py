"""GX6605S SPI transfers and a virtual serial NOR with optional volatile writes.

Controller bit fields and TX/RX packing follow xboot's GX6605S SPI driver.
TX packing latches ICNT on a FIFO write; changing ICNT before SPGO clocks a
prefix of that word, as required by the receiver kernel's JEDEC sequence.
Transfers complete synchronously; no frequency/timing model is provided.
"""
from collections import deque
from .memory import EmulationStop


class SerialNOR:
    def __init__(self, image, capacity=4 * 1024 * 1024, writable=False):
        if len(image) > capacity:
            raise ValueError("update payload exceeds virtual flash capacity")
        self.data = bytes(image) + b"\xff" * (capacity - len(image))
        self.read_bytes = 0
        self.jedec = bytes.fromhex("ef4016")  # virtual W25Q32-class device, not the receiver's measured identity
        self.transactions = deque(maxlen=32)
        self.security = {}  # explicit 1024-byte banks at 0x1000/0x2000/0x3000
        self.unique_id = None  # factory ID is separate from the main flash array
        self.writable = writable
        self.write_enable = False
        self.overlay = {}
        self.program_operations = self.erase_operations = 0
        self.select(False)

    def select(self, selected):
        if not selected and getattr(self, 'selected', False) and self.command in (0x02, 0x20, 0xd8):
            if len(self.address_bytes) != 3 or not self.write_enable:
                raise EmulationStop('invalid-flash-write', 'virtual flash mutation requires WREN and a complete address')
            address = int.from_bytes(bytes(self.address_bytes), 'big')
            if address >= len(self.data):
                raise EmulationStop('invalid-flash-write', 'virtual flash address exceeds capacity')
            if self.command == 0x02:
                if not self.program_data:
                    raise EmulationStop('invalid-flash-write', 'empty page program')
                page = address & ~255
                for i, byte in enumerate(self.program_data):
                    target = page | ((address + i) & 255)
                    self.overlay[target] = self.overlay.get(target, self.data[target]) & byte
                self.program_operations += 1
            else:
                block_size = 65536 if self.command == 0xd8 else 4096
                sector = address & ~(block_size - 1)
                for target in range(sector, min(sector + block_size, len(self.data))):
                    self.overlay[target] = 255
                self.erase_operations += 1
            self.write_enable = False
        self.selected = selected
        self.command = None
        self.address_bytes = []
        self.position = 0
        self.dummy = 0
        self.program_data = bytearray()
        self.unique_id_position = 0

    def exchange(self, value, lanes=1):
        if lanes not in (1, 2):
            raise ValueError("serial NOR supports one or two receive lanes")
        if not self.selected:
            return 0xff
        if self.command is None:
            if self.writable and value in (0x06, 0x04, 0x02, 0x20, 0xd8):
                if value in (0x02, 0x20, 0xd8) and not self.write_enable:
                    raise EmulationStop('invalid-flash-write', 'virtual flash program/erase without WREN')
                self.command = value
                self.transactions.append({'command': hex(value), 'address': None})
                if value in (0x06, 0x04):self.write_enable = value == 0x06
                return 255
            if value not in (0x03, 0x0b, 0x3b, 0x05, 0x35, 0x9f, 0x48, 0x4b):
                raise EmulationStop("unsupported-flash-command", f"NOR command=0x{value:02x}; operation unsupported or virtual writes disabled")
            self.command = value
            self.transactions.append({"command": hex(value), "address": None})
            return 0xff
        if self.command in (0x06, 0x04):
            raise EmulationStop('invalid-flash-write', 'extra clocks after virtual write-enable/disable command')
        if self.command in (0x05, 0x35):
            return 2 if self.command == 0x05 and self.write_enable else 0
        if self.command == 0x9f:
            result = self.jedec[self.position] if self.position < len(self.jedec) else 0xff
            self.position += 1
            return result
        if len(self.address_bytes) < 3:
            self.address_bytes.append(value)
            if len(self.address_bytes) == 3:
                self.position = int.from_bytes(bytes(self.address_bytes), "big")
                self.dummy = 8 if self.command in (0x0b, 0x3b, 0x48, 0x4b) else 0
                self.transactions[-1]["address"] = hex(self.position)
            return 0xff
        if self.command == 0x02:
            if len(self.program_data) >= 256:
                raise EmulationStop('unsupported-flash-write', 'page program exceeds 256 bytes')
            self.program_data.append(value)
            return 255
        if self.command in (0x20, 0xd8):
            raise EmulationStop('invalid-flash-write', 'extra data after sector erase address')
        if self.dummy:
            # Dummy latency is measured in clock cycles. In dual receive mode
            # each FIFO byte represents four clocks, so eight dummy clocks
            # occupy two FIFO bytes, not one. This matters for 0x3B reads.
            self.dummy = max(0, self.dummy - 8 // lanes)
            return 0xff
        if self.command == 0x48:
            bank = self.position & ~0x3ff
            if bank not in (0x1000, 0x2000, 0x3000):
                raise EmulationStop('unsupported-flash-security-address', 'GD25Q32 security address 0x%06x' % self.position)
            if bank not in self.security:
                raise EmulationStop('missing-flash-security-data',
                    'GD25Q32 read 48H at 0x%06x requires a captured 1024-byte security bank; these bytes are outside the 4 MiB flash image' % self.position)
            result = self.security[bank][self.position & 0x3ff]
            self.position = bank | ((self.position + 1) & 0x3ff)
            return result
        if self.command == 0x4b:
            if self.position != 0 or self.unique_id_position >= 16:
                raise EmulationStop('unsupported-flash-unique-id-read', 'GD25Q32 unique ID requires zero address and at most 16 captured bytes')
            if self.unique_id is None:
                raise EmulationStop('missing-flash-unique-id', 'GD25Q32 4BH requires the captured 16-byte factory ID, which is outside the main flash image')
            result = self.unique_id[self.unique_id_position]
            self.unique_id_position += 1
            return result
        result = self.overlay.get(self.position, self.data[self.position]) if self.position < len(self.data) else 0xff
        self.position = (self.position + 1) & 0xffffff
        self.read_bytes += 1
        return result


class SPIController:
    base = 0x00302000
    size = 0x10
    START = 1 << 12
    DUAL_RECEIVE = 1 << 22  # inferred from the bootloader's 0x3B transfer sequence

    def __init__(self, flash):
        self.flash = flash
        self.control = self.tx = self.rx = self.status = 0
        self.tx_bytes = bytes(4)
        self.transfers = 0

    def access(self, offset, size, value=None):
        if size == 1 and offset == 8 and value is not None:
            self.tx = value & 0xff
            self.tx_bytes = bytes([self.tx])
            return
        if size == 1 and offset == 12 and value is None:
            return self.rx & 0xff
        if size != 4 or offset not in (0, 4, 8, 12):
            raise EmulationStop("unsupported-device-access", f"SPI offset=0x{offset:x} size={size}")
        if value is None:
            return {0: self.control, 4: self.status, 8: self.tx, 12: self.rx}[offset]
        value &= 0xffffffff
        if offset == 8:
            self.tx = value
            count = ((self.control >> 14) & 3) + 1
            self.tx_bytes = (value & ((1 << (count * 8)) - 1)).to_bytes(count, 'big')
        elif offset == 0:
            selected = (value & 0x30000) == 0x30000
            if selected != self.flash.selected:
                self.flash.select(selected)
            start = bool(value & self.START) and not self.control & self.START
            if start:
                if value & (1 << 20):
                    raise EmulationStop("unsupported-device-access", "SPI LSB-first transfers are not implemented")
                count = ((value >> 14) & 3) + 1
                # Keep the latched word for repeated transfers. A receive-only
                # transfer may clock more bytes than were written; MOSI is zero
                # beyond the written prefix (only dummy clocks are supported).
                tx = self.tx_bytes.ljust(count, b'\0')[:count]
                lanes = 2 if value & self.DUAL_RECEIVE else 1
                received = bytes(self.flash.exchange(byte, lanes) for byte in tx)
                self.rx = int.from_bytes(received, "big")
                self.status = 1  # operation ready, BUSY clear
                self.transfers += 1
            self.control = value
        else:
            raise EmulationStop("unsupported-device-access", f"SPI register 0x{offset:x} is read-only")

    def snapshot(self):
        return {"control": hex(self.control), "status": hex(self.status), "transfers": self.transfers,
                "flash_size": len(self.flash.data), "virtual_jedec_id": self.flash.jedec.hex(), "flash_read_bytes": self.flash.read_bytes,
                "recent_commands": list(self.flash.transactions),
                'virtual_flash': {'writable_overlay': self.flash.writable, 'write_enable': self.flash.write_enable,
                                  'changed_bytes': len(self.flash.overlay), 'program_operations': self.flash.program_operations,
                                  'erase_operations': self.flash.erase_operations,
                                  'persistent_host_or_receiver_writes': False}}
