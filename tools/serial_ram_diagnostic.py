#!/usr/bin/env python3
"""Send a fixed UART-only diagnostic to an already waiting GX6605S RAM stage 1.

No vendor stage-2 monitor or flash command is uploaded. Our guest code reads
four CPU control registers, stores them in its own RAM buffer, sends that
buffer through the already initialized UART, and loops. Power cycling restores
flash boot. A successful serial_ram_probe report is required and claimed once.
"""
import argparse
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import sys
import termios
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from starsat_emu.cpu import CPU, decode
from starsat_emu.memory import Bus
from starsat_emu.mmu import TLBEntry
from serial_capture import utc_now
from serial_ram_probe import LOADER_SHA256, stage1_packet

ENTRY = 0x93f40000
MAGIC = b"STAR-RAM1" + bytes(7)
FOOTER = b"END-RAM1"
REPLY_SIZE = len(MAGIC) + 16 + len(FOOTER)
REGISTERS = (0, 17, 18, 30)


def diagnostic():
    """ABIv1 machine code; no stack, arbitrary jumps, or external memory writes."""
    words, literals, branches, labels = [], [], [], {}

    def literal(reg, name):
        literals.append((len(words), reg, name))
        words.append(0)

    def branch(op, label):
        branches.append((len(words), op, label))
        words.append(0)

    literal(4, "buffer")
    for i, reg in enumerate(REGISTERS):
        words.append(0x1008 | (reg << 4))  # mfcr r8, crN
        words.append(0x9804 | ((4 + i) << 4))  # st.w r8, (r4, 16+4*i)
    literal(3, "uart")
    words.append(0x6005 | (REPLY_SIZE << 4))  # movi r5, reply size
    labels["send"] = len(words)
    words.extend([0x8653, 0x6407, 0x1676, 0x2a06])  # LSR, mask 0x40, compare
    branch("bf", "send")
    words.extend([0xa204, 0x9203, 0x2004, 0x2405, 0x2a05])
    branch("bt", "send")
    labels["idle"] = len(words)
    branch("br", "idle")
    if len(words) % 2:
        words.append(0x1200)  # align the literal pool
    pool = {"uart": len(words) * 2, "buffer": len(words) * 2 + 4}
    data_offset = len(words) * 2 + 8
    for index, reg, name in literals:
        pc = ENTRY + index * 2
        target = ENTRY + pool[name]
        displacement = (target - ((pc + 2) & ~3)) // 4
        if not 0 <= displacement <= 255:
            raise ValueError("literal outside ABIv1 range")
        words[index] = 0x7000 | reg << 8 | displacement
        assert decode(words[index], pc).args == (reg, target)
    for index, op, label in branches:
        displacement = labels[label] - index - 1
        if not -1024 <= displacement < 1024:
            raise ValueError("branch outside ABIv1 range")
        words[index] = {"br": 0xf000, "bt": 0xe000, "bf": 0xe800}[op] | (displacement & 0x7ff)
    code = struct.pack("<" + "H" * len(words), *words)
    code += struct.pack("<II", 0xa0403000, ENTRY + data_offset)
    code += MAGIC + bytes(16) + FOOTER
    return code, ENTRY + labels["idle"] * 2


def stage2_packet():
    code, _ = diagnostic()
    return wrap_stage2(code)


def wrap_stage2(code):
    # Stage 1 discards the first 8192 bytes, copies 32 metadata bytes to
    # 0x90100000, then copies the rest to ENTRY. Last metadata word is code size.
    content = bytes(8192 + 28) + struct.pack("<I", len(code)) + code
    return struct.pack("<II", sum(content) & 0xffffffff, len(content)) + content


def parse_reply(data):
    n = data.find(MAGIC)
    if n < 0 or len(data) < n + REPLY_SIZE:
        return None
    block = data[n:n + REPLY_SIZE]
    if block[-len(FOOTER):] != FOOTER:
        raise ValueError("RAM diagnostic footer mismatch")
    values = struct.unpack_from("<4I", block, len(MAGIC))
    return {f"cr{reg}": f"0x{value:08x}" for reg, value in zip(REGISTERS, values)}


class DiagnosticBus(Bus):
    """Offline-only bounded UART fixture; all other MMIO is rejected."""
    def __init__(self, incoming=b""):
        super().__init__()
        self.incoming = deque(incoming)
        self.outgoing = bytearray()
        self.device_writes = []

    def access(self, address, size, value=None, fetch=False):
        if not fetch and address == 0x403014 and value is None:
            return 0x60 | bool(self.incoming)
        if not fetch and address == 0x403000:
            if value is None:
                if not self.incoming:
                    raise ValueError("diagnostic fixture UART input exhausted")
                return self.incoming.popleft()
            self.outgoing.append(value & 255)
            self.device_writes.append((address, value, size))
            return None
        # The existing first-stage epilogue writes 1 to this OTP controller
        # configuration register. The custom diagnostic itself never does.
        if not fetch and address == 0xf80004 and size == 4 and value == 1:
            self.device_writes.append((address, value, size))
            return None
        return super().access(address, size, value, fetch)


def simulate_continuation(loader, code, idle, bus, limit=1000000):
    """Run unchanged stage-1 receive/copy/checksum logic into a supplied RAM program."""
    stage1_packet(loader)
    bus.incoming.extend(wrap_stage2(code))
    bus.map("stage1-sram", 0x100000, 0x10000, loader[32:0x201c])
    bus.map("dram", 0x10000000, 64 * 1024 * 1024)
    cpu = CPU(bus, 0x100760)
    cpu.cr[18] = 0xfd
    cpu.mmu.entries[0] = TLBEntry(0, 0, 0x1fe000, 20, 0, 0x401f)
    cpu.regs[0] = 0x93f3ffd0
    cpu.write(cpu.regs[0] + 16 + 28, 0x1000ce)
    while cpu.steps < limit and cpu.pc != idle:
        cpu.step()
    if cpu.pc != idle or bus.incoming or bytes(bus.outgoing[:1]) != b"O":
        raise ValueError("offline stage-1 continuation did not reach the program idle loop")
    return cpu


def check_with_stage1(loader):
    """Execute the unchanged post-RUNGET receiver loop plus our code locally.

    This validates placement, checksum and guest output with modeled UART/RAM;
    it does not emulate the earlier clock/DDR initialization or hardware timing.
    """
    code, idle = diagnostic()
    bus = DiagnosticBus()
    cpu = simulate_continuation(loader, code, idle, bus)
    parsed = parse_reply(bytes(bus.outgoing))
    if parsed is None or len(bus.outgoing) != REPLY_SIZE + 1:
        raise ValueError("offline diagnostic reply invalid")
    if any(a != 0x403000 and (a, v, z) != (0xf80004, 1, 4) for a, v, z in bus.device_writes):
        raise ValueError("unexpected peripheral write")
    return {"steps": cpu.steps, "reply_hex": bus.outgoing.hex(), "registers": parsed,
            "limitations": "post-RUNGET modeled UART/RAM; clock/DDR startup and wire timing excluded"}


def run(port, ready_path, loader_path, prefix, *, packet_factory=None, verifier=None,
        reply_parser=None, mode="custom-uart-only-ram-diagnostic", seconds=10,
        result_key="registers", success_reason="diagnostic-executed", on_data=None):
    packet_factory = packet_factory or stage2_packet
    verifier = verifier or check_with_stage1
    reply_parser = reply_parser or parse_reply
    if not 0 < seconds <= 1800:
        raise ValueError("reply timeout must be within 0..1800 seconds")
    ready_path, prefix = Path(ready_path), Path(prefix)
    ready = json.loads(ready_path.read_text())
    if ready.get("reason") != "stage1-ready" or ready.get("port") != port or ready.get("loader_sha256") != LOADER_SHA256:
        raise ValueError("requires a successful stage-1 report for this serial port and pinned loader")
    raw_ready = ready_path.with_suffix(".bin").read_bytes()
    if raw_ready != b"\0XRUNGET":
        raise ValueError("stage-1 capture does not contain the verified handshake")
    offline = verifier(Path(loader_path).read_bytes())
    packet = packet_factory()
    raw_path, report_path = Path(str(prefix) + ".bin"), Path(str(prefix) + ".json")
    if any(p.exists() or p.is_symlink() for p in (raw_path, report_path)):
        raise FileExistsError("diagnostic output already exists")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report = {"mode": mode, "port": port, "started_utc": utc_now(),
              "stage1_report": str(ready_path), "offline_check": offline, "packet_sha256": hashlib.sha256(packet).hexdigest(),
              "planned_bytes": len(packet), "serial_bytes_sent": 0, "bytes_received": 0,
              "flash_commands_sent": False, "flash_commands_sent_scope": "direct host commands; see guest_flash_read_commands for RAM code",
              "guest_flash_read_commands": (["9f", "48"] if mode == "read-only-gd25q32-security-bank" else ["9f", "03"] if mode == "read-only-gd25q32-ram-dump" else ['4b','48'] if mode == 'fixed-ram-graphics-copy-irq-probe' else []),
              "flash_programming_commands_sent": False, "vendor_stage2_monitor_sent": False, "reason": "error"}
    with ExitStack() as stack:
        raw = stack.enter_context(raw_path.open("xb"))
        metadata = stack.enter_context(report_path.open("x"))
        # Prevent accidentally sending another continuation to this boot state.
        stack.enter_context(Path(str(ready_path) + ".continued").open("x"))
        fd, old = None, None
        received = bytearray()
        try:
            fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
            old = termios.tcgetattr(fd)
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL)
            for flag in ("CRTSCTS", "CCTS_OFLOW", "CRTS_IFLOW"):
                attrs[2] &= ~getattr(termios, flag, 0)
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            # Never flush device output here: stage-1 state was established by
            # the preceding capture, and any new output may invalidate it.
            if select.select([fd], [], [], 0.1)[0]:
                pending = os.read(fd, 4096)
                if pending:
                    raw.write(pending)
                    received.extend(pending)
                    raise ValueError("receiver produced new output since RUNGET; refusing a stale continuation")
            print("Sending fixed RAM diagnostic (%d bytes); %s." % (len(packet),
                  "read-only flash reads via RAM; no programming commands" if report["guest_flash_read_commands"] else "no flash commands"), flush=True)
            deadline = time.monotonic() + max(5, len(packet) * 10 / 115200 + 5)
            while report["serial_bytes_sent"] < len(packet):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                    raise TimeoutError("diagnostic upload timed out")
                try:
                    n = os.write(fd, packet[report["serial_bytes_sent"]:])
                except BlockingIOError:
                    continue
                if n <= 0:
                    raise OSError("diagnostic write made no progress")
                report["serial_bytes_sent"] += n
            deadline = time.monotonic() + seconds
            report["reason"] = "reply-timeout"
            while time.monotonic() < deadline:
                if not select.select([fd], [], [], min(0.1, max(0, deadline - time.monotonic())))[0]:
                    continue
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    report["reason"] = "device-disconnected"
                    break
                raw.write(data)
                raw.flush()
                received.extend(data)
                if on_data is not None:
                    on_data(received)
                result = reply_parser(received)
                if result is not None:
                    if received[:1] != b"O":
                        raise ValueError("missing stage-1 checksum acknowledgment")
                    report.update(reason=success_reason)
                    report[result_key] = result
                    break
                if received[:1] == b"E":
                    report["reason"] = "stage1-checksum-rejected"
                    break
        except KeyboardInterrupt:
            report["reason"] = "interrupted"
        except Exception as exc:
            report.update(reason="error", error=str(exc))
            raise
        finally:
            if fd is not None:
                try:
                    if old is not None:
                        termios.tcsetattr(fd, termios.TCSANOW, old)
                except termios.error as exc:
                    report["restore_error"] = str(exc)
                finally:
                    os.close(fd)
            report["ended_utc"] = utc_now()
            report["bytes_received"] = len(received)
            report["reply_hex"] = received.hex()
            json.dump(report, metadata, indent=2)
            metadata.write("\n")
    print("Result: %s. Report: %s" % (report["reason"], report_path), flush=True)
    if report.get(result_key):
        visible = report[result_key]
        if result_key == 'graphics':
            visible = {k:v for k,v in visible.items() if k != 'flash_uid_hex'}
            if 'security_banks' in visible:
                visible['security_banks'] = {bank:{k:v for k,v in entry.items() if k != 'data_hex'}
                                             for bank,entry in visible['security_banks'].items()}
        print(json.dumps(visible, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--ready-report", type=Path, required=True)
    parser.add_argument("--loader", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-ram-diagnostic-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    try:
        report = run(args.port, args.ready_report, args.loader, args.output)
    except (OSError, ValueError, termios.error) as exc:
        print("RAM diagnostic failed: %s" % exc, file=sys.stderr)
        return 2
    return 0 if report["reason"] == "diagnostic-executed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
