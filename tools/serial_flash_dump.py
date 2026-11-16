#!/usr/bin/env python3
"""Fixed read-only 4 MiB GD25Q32 RAM reader, following a fresh stage-1 handshake.

The default reader uses only 9F (JEDEC ID) and 03 (read at address zero).
--security-bank 0x3000 instead reads only that separate 1024-byte bank with
9F and 48H; it does not read or alter the main array or other security banks.
No WREN, program, erase, status-register write, or vendor monitor is sent.
The reader computes CRC32 in RAM, deselects flash, sends the framed dump over
UART, and loops. Power cycling restores normal flash startup.
"""
from collections import deque
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from starsat_emu.cpu import CPU
from starsat_emu.spi import SPIController, SerialNOR
from csky_ram_asm import Assembler
import serial_ram_diagnostic as ram
import serial_ram_probe as stage1

SIZE = 4 * 1024 * 1024
MAGIC = b"STAR-FLASH1".ljust(16, b"\0")
FOOTER = b"END-FLS1"
SECURITY_MAGIC = b"STAR-SECR1".ljust(16, b"\0")
SECURITY_ALL_MAGIC = b"STAR-SECR2".ljust(16, b"\0")
SECURITY_BANK = 0x3000  # only the bank requested by both observed kernels
SECURITY_SIZE = 1024
SECURITY_BANKS = (0x1000, 0x2000, 0x3000)
HEADER_SIZE = 48
JEDEC = 0xc84016
SPI_OFF = 0x8420c01f  # observed controller setup, with slowest SPBR divider


def crc_table():
    words = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0xedb88320 if value & 1 else 0)
        words.append(value)
    return struct.pack("<256I", *words)


def reader(size=SIZE, poll_budget=1 << 20, security_bank=None, security_all=False):
    if type(size) is not int or not 4 <= size <= SIZE or size % 4:
        raise ValueError("read size must be 4-byte aligned and within 4 MiB")
    if type(poll_budget) is not int or not 1 <= poll_budget <= 1 << 20:
        raise ValueError("invalid SPI polling budget")
    if security_bank is not None and (type(security_bank) is not int or security_bank not in SECURITY_BANKS or size != SECURITY_SIZE):
        raise ValueError('security capture requires a valid 1024-byte bank')
    if type(security_all) is not bool or (security_all and (security_bank is not None or size != 3*SECURITY_SIZE)):
        raise ValueError('all-bank capture requires exactly three banks and no single-bank option')
    magic = SECURITY_ALL_MAGIC if security_all else SECURITY_MAGIC if security_bank is not None else MAGIC
    a = Assembler(ram.ENTRY)
    e = a.emit
    e("lrw", 3, 0xa0403000)
    e("lrw", 10, "header")
    e("mov", 4, 10)
    e("movi", 5, len(magic))
    e("bsr", "send")  # prove entry before probing any SPI register
    e("lrw", 13, 0xa0302000)
    e("load", 6, 13, 0, 4)
    e("store", 6, 10, 32, 4)
    e("load", 6, 13, 4, 4)
    e("store", 6, 10, 36, 4)
    e("lrw", 14, SPI_OFF)
    e("store", 14, 13, 0, 4)
    e("lrw", 14, (SPI_OFF & ~0xc000) | 0x30000)  # one byte, selected
    e("lrw", 2, 0x9f)
    e("bsr", "transfer")
    e("lrw", 14, (SPI_OFF & ~0xc000) | 0x38000)  # three bytes, selected
    e("lrw", 2, 0xffffff)
    e("bsr", "transfer")
    e("store", 2, 10, 24, 4)
    e("lrw", 7, JEDEC)
    e("cmpne", 2, 7)
    e("bt", "bad-id")
    if security_all:
        e('lrw',1,0x1000)
        e('lrw',11,0x90000000);e('lrw',12,0x90000400)
        e('lrw',8,0xffffffff);e('lrw',9,'crc-table')
        a.label('security-next')
    e("lrw", 14, SPI_OFF)
    e("store", 14, 13, 0, 4)
    e("lrw", 14, SPI_OFF | 0x30000)  # four bytes, selected
    e("lrw", 2, 0x48000000 if security_all else 0x03000000 if security_bank is None else 0x48000000 | security_bank)
    if security_all:e('or',2,1)
    e("bsr", "transfer")
    if security_bank is not None or security_all:
        e('lrw', 14, (SPI_OFF & ~0xc000) | 0x30000)
        e('lrw', 2, 0xff)  # 48H requires eight dummy clocks after its address
        e('bsr', 'transfer')
        e('lrw', 14, SPI_OFF | 0x30000)
    if not security_all:
        e("lrw", 11, 0x90000000)
        e("lrw", 12, 0x90000000 + size)
        e("lrw", 8, 0xffffffff)
        e("lrw", 9, "crc-table")
    a.label("read")
    e("lrw", 2, 0xffffffff)
    e("bsr", "transfer")
    for shift in (24, 16, 8, 0):
        e("mov", 6, 2)
        if shift:
            e("lsri", 6, shift)
        e("zextb", 6)
        e("store", 6, 11, 0, 1)
        e("addi", 11, 1)
        e("mov", 7, 8)
        e("xor", 7, 6)
        e("zextb", 7)
        e("lsli", 7, 2)
        e("addu", 7, 9)
        e("load", 7, 7, 0, 4)
        e("lsri", 8, 8)
        e("xor", 8, 7)
    e("cmpne", 11, 12)
    e("bt", "read")
    if security_all:
        e('lrw',6,0x3000);e('cmpne',1,6);e('bf','security-complete')
        e('lrw',6,0x1000);e('addu',1,6)
        e('lrw',6,0x400);e('addu',12,6);e('br','security-next')
        a.label('security-complete')
    e("lrw", 14, SPI_OFF)
    e("store", 14, 13, 0, 4)  # deselect before slow UART transfer
    e("not", 8)
    e("store", 8, 10, 40, 4)
    a.label("report")
    e("mov", 4, 10)
    e("addi", 4, len(MAGIC))
    e("movi", 5, HEADER_SIZE - len(MAGIC))
    e("bsr", "send")
    e("load", 7, 10, 28, 4)
    e("cmpnei", 7, 0)
    e("bt", "footer")
    e("lrw", 4, 0x90000000)
    e("lrw", 5, size)
    e("bsr", "send")
    a.label("footer")
    e("lrw", 4, "footer-data")
    e("movi", 5, len(FOOTER))
    e("bsr", "send")
    a.label("idle")
    e("br", "idle")
    a.label("bad-id")
    e("movi", 7, 2)
    e("br", "fail")
    a.label("timeout")
    e("movi", 7, 1)
    a.label("fail")
    e("store", 7, 10, 28, 4)
    e("lrw", 14, SPI_OFF)
    e("store", 14, 13, 0, 4)
    e("br", "report")
    a.label("transfer")
    e("store", 14, 13, 0, 4)
    e("store", 2, 13, 8, 4)
    e("mov", 6, 14)
    e("bseti", 6, 12)
    e("store", 6, 13, 0, 4)
    e("lrw", 7, poll_budget)
    a.label("poll")
    e("load", 6, 13, 4, 4)
    e("btsti", 6, 0)
    e("bt", "ready")
    e("subi", 7, 1)
    e("cmpnei", 7, 0)
    e("bt", "poll")
    e("br", "timeout")
    a.label("ready")
    e("store", 14, 13, 0, 4)
    e("load", 2, 13, 12, 4)
    e("jmp", 15)
    a.label("send")
    e("load", 6, 3, 20, 4)
    e("movi", 7, 64)
    e("and", 6, 7)
    e("cmpnei", 6, 0)
    e("bf", "send")
    e("load", 2, 4, 0, 1)
    e("store", 2, 3, 0, 4)
    e("addi", 4, 1)
    e("subi", 5, 1)
    e("cmpnei", 5, 0)
    e("bt", "send")
    e("jmp", 15)
    header = magic + struct.pack("<8I", 1, size, 0, 0, 0, 0, 0, 0xffffffff if security_all else security_bank or 0)
    code = a.finish({"header": header, "footer-data": FOOTER, "crc-table": crc_table()})
    return code, a.labels["idle"]


def parse_frame(data, expected_size=SIZE, security_bank=None, security_all=False):
    if security_bank is not None and (security_bank not in SECURITY_BANKS or expected_size != SECURITY_SIZE):
        raise ValueError('unexpected security bank or capture size')
    if security_all and (security_bank is not None or expected_size != 3072):
        raise ValueError('unexpected all-bank capture size')
    if len(data) < 1 + HEADER_SIZE:
        return None
    magic = SECURITY_ALL_MAGIC if security_all else SECURITY_MAGIC if security_bank is not None else MAGIC
    if data[:1] != b"O" or data[1:17] != magic:
        raise ValueError("invalid RAM reader acknowledgment or framing")
    version, size, jedec, status, control, spi_status, crc, reserved = struct.unpack_from("<8I", data, 17)
    if version != 1 or size != expected_size or reserved != (0xffffffff if security_all else security_bank or 0):
        raise ValueError("unexpected flash reader version, size, or reserved field")
    if status:
        raise ValueError("flash reader failed: status=%d, JEDEC=%06x, initial control=%08x status=%08x" % (status, jedec, control, spi_status))
    if jedec != JEDEC:
        raise ValueError("unexpected flash identity")
    total = 1 + HEADER_SIZE + size + len(FOOTER)
    if len(data) < total:
        return None
    if len(data) != total or data[-len(FOOTER):] != FOOTER:
        raise ValueError("invalid flash dump length or footer")
    content = memoryview(data)[1 + HEADER_SIZE:1 + HEADER_SIZE + size]
    actual_crc = zlib.crc32(content)
    if actual_crc != crc:
        raise ValueError("flash dump CRC32 mismatch: receiver=%08x host=%08x" % (crc, actual_crc))
    result = {"size": size, "jedec": "%06x" % jedec, "crc32": "%08x" % crc,
            "sha256": hashlib.sha256(content).hexdigest(),
            "initial_spi_control": "0x%08x" % control, "initial_spi_status": "0x%08x" % spi_status}
    if security_bank is not None:
        result['security_bank'] = hex(security_bank)
    if security_all:
        result['security_banks'] = {hex(bank):{'size':1024,'crc32':'%08x'%zlib.crc32(content[i*1024:(i+1)*1024]),
                                               'sha256':hashlib.sha256(content[i*1024:(i+1)*1024]).hexdigest()}
                                    for i,bank in enumerate(SECURITY_BANKS)}
    return result


class ReaderBus(ram.DiagnosticBus):
    """Offline reader fixture: strict NOR rejects every non-read command."""
    def __init__(self, image, jedec=JEDEC, stalled=False, security=None):
        super().__init__()
        self.spi = SPIController(SerialNOR(image))
        self.spi.flash.jedec = jedec.to_bytes(3, "big")
        self.allowed_commands = (0x9f, 0x03) if security is None else (0x9f, 0x48)
        if security is not None:
            if isinstance(security,dict):self.spi.flash.security.update(security)
            else:self.spi.flash.security[SECURITY_BANK] = bytes(security)
        self.stalled = stalled
        self.commands = []

    def access(self, address, size, value=None, fetch=False):
        if not fetch and 0x302000 <= address < 0x302010:
            if value is not None:
                if address not in (0x302000, 0x302008):
                    raise ValueError("reader attempted unexpected SPI register write")
                self.device_writes.append((address, value, size))
            if self.stalled and address == 0x302004 and value is None:
                return 0
            before = self.spi.flash.command
            result = self.spi.access(address - 0x302000, size, value)
            if before is None and self.spi.flash.command is not None:
                self.commands.append(self.spi.flash.command)
                if self.spi.flash.command not in self.allowed_commands:
                    raise ValueError("unexpected flash command")
            return result
        return super().access(address, size, value, fetch)


def check_reader(loader):
    # Same instruction path as 4 MiB; 4096-byte fixture exercises every byte,
    # CRC-table index, FIFO word packing, frame field, and flash-CS boundary.
    image = bytes(range(256)) * 16
    code, idle = reader(len(image))
    bus = ReaderBus(image)
    cpu = ram.simulate_continuation(loader, code, idle, bus, limit=2000000)
    result = parse_frame(bus.outgoing, len(image))
    if bus.outgoing[1 + HEADER_SIZE:-len(FOOTER)] != image or bus.commands != [0x9f, 0x03] or bus.spi.flash.selected:
        raise ValueError("offline reader data/commands/chip-select check failed")
    return {"steps": cpu.steps, "fixture_bytes": len(image), "commands": ["9f", "03"],
            "flash_deselected": True, "result": result,
            "limitations": "4096-byte modeled NOR/UART fixture; real pinmux, clocks, electrical timing not modeled"}


def check_security_reader(loader, all_banks=False):
    banks={bank:bytes((i*17+91+bank//256)&255 for i in range(1024)) for bank in SECURITY_BANKS}
    image=b''.join(banks.values()) if all_banks else banks[SECURITY_BANK]
    code, idle = reader(len(image), security_bank=None if all_banks else SECURITY_BANK, security_all=all_banks)
    bus = ReaderBus(b'', security=banks if all_banks else image)
    cpu = ram.simulate_continuation(loader, code, idle, bus, limit=2000000)
    result = parse_frame(bus.outgoing, len(image), None if all_banks else SECURITY_BANK, all_banks)
    if bus.outgoing[1 + HEADER_SIZE:-len(FOOTER)] != image or bus.commands != [0x9f]+[0x48]*(3 if all_banks else 1) or bus.spi.flash.selected:
        raise ValueError('offline security reader data/commands/chip-select check failed')
    return {'steps': cpu.steps, 'fixture_bytes': len(image), 'commands': ['9f']+['48']*(3 if all_banks else 1),
            'security_banks':list(result.get('security_banks',{})) if all_banks else [hex(SECURITY_BANK)], 'flash_deselected': True, 'result': result,
            'limitations': 'modeled fixture; real pinmux, clocks and electrical timing not modeled'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--loader", type=Path, required=True)
    bank_group=parser.add_mutually_exclusive_group()
    bank_group.add_argument('--security-bank', type=lambda text: int(text, 0), choices=SECURITY_BANKS,
                        help='read only the separate 1024-byte security bank requested by the native kernel')
    bank_group.add_argument('--security-all',action='store_true',help='read the three separate security banks with no graphics or interrupt configuration')
    parser.add_argument("--seconds", type=float, default=600, help="arming window; receiver must already be off")
    parser.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-flash-dump-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    try:
        # Complete the offline check BEFORE asking the user to reconnect power.
        print("Checking read-only RAM reader locally...", flush=True)
        security = args.security_bank is not None or args.security_all
        size = 3072 if args.security_all else SECURITY_SIZE if security else SIZE
        magic = SECURITY_ALL_MAGIC if args.security_all else SECURITY_MAGIC if security else MAGIC
        verifier = (lambda loader:check_security_reader(loader,True)) if args.security_all else check_security_reader if security else check_reader
        offline = verifier(args.loader.read_bytes())
        print("Local checksum/data/command checks passed.", flush=True)
        boot_prefix = Path(str(args.output) + "-stage1")
        boot = stage1.probe(args.port, args.loader, args.seconds, boot_prefix)
        if boot["reason"] != "stage1-ready":
            return 3
        last = [-1]

        def progress(data):
            if len(data) >= 17 and data[1:17] == magic and last[0] < 0:
                print("RAM reader started; reading flash into RAM...", flush=True)
                last[0] = 0
            if len(data) >= 1 + HEADER_SIZE:
                count = max(0, min(size, len(data) - 1 - HEADER_SIZE))
                if count // 262144 > last[0] // 262144:
                    print("Flash transfer: %.1f%% (%d / %d bytes)" % (count * 100 / size, count, size), flush=True)
                    last[0] = count

        report = ram.run(args.port, Path(str(boot_prefix) + ".json"), args.loader, args.output,
                         packet_factory=lambda: ram.wrap_stage2(reader(size, security_bank=args.security_bank, security_all=args.security_all)[0]),
                         verifier=lambda loader: offline if hashlib.sha256(loader).hexdigest() == stage1.LOADER_SHA256 else verifier(loader),
                         reply_parser=lambda data: parse_frame(data, size, args.security_bank, args.security_all),
                         mode='read-only-gd25q32-security-bank' if security else "read-only-gd25q32-ram-dump", seconds=30 if security else 900,
                         result_key="flash", success_reason="flash-dump-received", on_data=progress)
        if report["reason"] != "flash-dump-received":
            return 3
        raw = Path(str(args.output) + ".bin").read_bytes()
        image = raw[1 + HEADER_SIZE:1 + HEADER_SIZE + size]
        dump_path = Path(str(args.output) + ('.security-all.bin' if args.security_all else '.security-%04x.bin'%args.security_bank if security else '.flash.bin'))
        with dump_path.open("xb") as out:
            out.write(image)
        print("Verified read-only flash copy saved: %s" % dump_path, flush=True)
        return 0
    except (OSError, ValueError, ram.termios.error) as exc:
        print("Read-only flash dump failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
