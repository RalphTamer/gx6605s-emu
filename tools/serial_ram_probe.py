#!/usr/bin/env python3
"""GX6605S first-stage UART RAM handshake probe; never sends stage 2 or flash commands.

Protocol reference: https://github.com/rva3/gx6605s-loader (GX6605S)
Artifact reference: matu6968/libre-gxdl, commit
0cf702936ecb35234d116beff43c8039e67e48bb,
loaders/gx6605s-6605s-sflash-27M.boot. This proprietary artifact is not bundled.

This executes the pinned manufacturer's RAM bootstrap on the receiver. It
initializes clocks/DDR/UART and waits for stage 2 after RUNGET. It does not boot
the normal application in this test. A receiver power cycle returns to flash
boot. This first stage has returned RUNGET on the physical receiver; that
does not validate the larger vendor monitor or any flash-writing operation.
Disconnect receiver DC power before starting this tool; power it on only after
ARMED. Power-off transients can otherwise resemble the ROM handshake.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import sys
import termios
import time

from serial_capture import display_bytes, utc_now

LOADER_SHA256 = "0e9e4be311b445ae818585d93c889495fe0d33d794cb1966690bb17963a51631"
LOADER_SIZE = 148012
HEADER = bytes.fromhex("746f6f620001113200c20100")


def stage1_packet(loader):
    if len(loader) != LOADER_SIZE or hashlib.sha256(loader).hexdigest() != LOADER_SHA256:
        raise ValueError("loader does not match the pinned reviewed GX6605S artifact")
    if loader[:12] != HEADER:
        raise ValueError("unexpected GX6605S loader header")
    # 0x800 words, SRAM offset 0. Header is excluded; boot is the final word.
    return bytes.fromhex("5900080000") + loader[0x20:0x201c] + b"boot"


class Handshake:
    """Trigger only on the observed binary ROM prefix, before flash boot text."""
    def __init__(self):
        self.tail = b""
        self.state = "waiting-for-rom"

    def observe(self, data):
        joined = self.tail + data
        self.tail = joined[-64:]
        if self.state == "waiting-for-rom":
            if b"Partition Version" in joined:
                self.state = "missed-rom-window"
            elif b"\0X" in joined:
                self.state = "waiting-for-runget"
                return True
        elif self.state == "waiting-for-runget":
            if b"RUNGET" in joined:
                self.state = "stage1-ready"
            elif b"Partition Version" in joined:
                self.state = "flash-startup-continued"
        return False


def probe(port, loader_path, seconds, prefix, on_ready=None):
    if not 0 < seconds <= 7200:
        raise ValueError("arming duration must be 1..7200 seconds")
    packet = stage1_packet(Path(loader_path).read_bytes())
    prefix = Path(prefix)
    raw_path, json_path = Path(str(prefix) + ".bin"), Path(str(prefix) + ".json")
    if any(p.exists() or p.is_symlink() for p in (raw_path, json_path)):
        raise FileExistsError("capture outputs already exist")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report = {"mode": "gx6605s-stage1-ram-probe", "port": port, "baud": 115200,
              "started_utc": utc_now(), "loader_sha256": LOADER_SHA256,
              "bytes_received": 0, "serial_bytes_sent": 0, "tx_events": [],
              "stage2_sent": False, "flash_commands_sent": False,
              "raw_file": str(raw_path), "reason": "error"}
    with ExitStack() as stack:
        raw = stack.enter_context(raw_path.open("xb"))
        metadata = stack.enter_context(json_path.open("x"))
        fd, old = None, None
        handshake = Handshake()
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
            termios.tcflush(fd, termios.TCIFLUSH)
            print("ARMED: connect receiver DC power now (it must already be off). Stage 1 RAM handshake only; no stage 2 or flash commands.", flush=True)
            if on_ready is not None:
                on_ready()
            deadline = time.monotonic() + seconds
            report["reason"] = "arming-timeout"
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
                report["bytes_received"] += len(data)
                send = handshake.observe(data)
                if send:
                    # Respond before rendering/logging the boot banner.
                    start = time.monotonic()
                    event = {"utc": utc_now(), "header_hex": packet[:5].hex(),
                             "planned_bytes": len(packet), "written_bytes": 0,
                             "packet_sha256": hashlib.sha256(packet).hexdigest(),
                             "after_rx_bytes": report["bytes_received"]}
                    report["tx_events"].append(event)
                    while event["written_bytes"] < len(packet):
                        remaining = 5 - (time.monotonic() - start)
                        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                            raise TimeoutError("RAM bootstrap write timed out")
                        try:
                            count = os.write(fd, packet[event["written_bytes"]:])
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise OSError("RAM bootstrap write made no progress")
                        event["written_bytes"] += count
                        report["serial_bytes_sent"] += count
                    deadline = time.monotonic() + 15
                    report["reason"] = "runget-timeout"
                    print("\n[Sent %d bytes of stage-1 RAM bootstrap; waiting for RUNGET]" % len(packet), flush=True)
                print(display_bytes(data), end="", flush=True)
                if handshake.state in ("stage1-ready", "missed-rom-window", "flash-startup-continued"):
                    report["reason"] = handshake.state
                    break
        except KeyboardInterrupt:
            report["reason"] = "interrupted"
        except Exception as exc:
            report["reason"] = "error"
            report["error"] = str(exc)
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
            report["handshake_state"] = handshake.state
            json.dump(report, metadata, indent=2)
            metadata.write("\n")
    print("\nResult: %s; %d received, %d sent. Report: %s" % (
        report["reason"], report["bytes_received"], report["serial_bytes_sent"], json_path), flush=True)
    if report["reason"] == "stage1-ready":
        print("Bootstrap is waiting in RAM. No stage 2 was sent. Power-cycle receiver to return to its normal firmware.", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--loader", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-ram-probe-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    try:
        report = probe(args.port, args.loader, args.seconds, args.output)
    except (OSError, ValueError, termios.error) as exc:
        print("RAM probe failed: %s" % exc, file=sys.stderr)
        return 2
    return 0 if report["reason"] == "stage1-ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
