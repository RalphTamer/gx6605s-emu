#!/usr/bin/env python3
"""Capture physical serial input without sending bytes (macOS/POSIX).

This host diagnostic is separate from the emulator. Configure wiring before
opening a port: serial drivers can change modem-control lines on open/close.
Only receiver ground and the adapter's verified receive input should be wired.
The separate serial_boot_probe CLI can explicitly use the capture API's
BootKeyProbe, enabling one logged control-key transmission after a boot marker.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import glob
import json
import math
import os
from pathlib import Path
import select
import sys
import termios
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def display_bytes(data):
    """Show input without executing terminal escape/control sequences."""
    return "".join(chr(b) if b in (9, 10, 13) or 32 <= b < 127
                   else "\\x%02x" % b for b in data)


class BootKeyProbe:
    """One control key per capture, triggered by this receiver's boot banner."""
    KEYS = {"escape": b"\x1b", "enter": b"\r", "space": b" ", "ctrl-c": b"\x03"}
    marker = b"Partition Version"

    def __init__(self, key="escape"):
        if key not in self.KEYS:
            raise ValueError("unsupported boot key")
        self.key = key
        self.tail = b""
        self.fired = False

    def observe(self, data):
        if self.fired:
            return b""
        joined = self.tail + data
        self.tail = joined[-(len(self.marker) - 1):]
        if self.marker not in joined:
            return b""
        self.fired = True
        return self.KEYS[self.key]


def capture(port, baud, duration, prefix, label="", on_ready=None, probe=None):
    speed = getattr(termios, "B%d" % baud, None)
    if speed is None or baud <= 0:
        raise ValueError("unsupported baud rate")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and positive")
    if probe is not None and (type(probe) is not BootKeyProbe or probe.fired):
        raise ValueError("probe must be a fresh BootKeyProbe")
    prefix = Path(prefix)
    raw_path, report_path = Path(str(prefix) + ".bin"), Path(str(prefix) + ".json")
    if any(p.exists() or p.is_symlink() for p in (raw_path, report_path)):
        raise FileExistsError("capture outputs already exist; choose another prefix")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report = {"port": str(port), "baud": baud, "format": "8N1", "flow_control": "none",
              "label": label, "started_utc": utc_now(), "bytes_received": 0,
              "serial_bytes_sent": 0, "raw_file": str(raw_path), "reason": "error"}
    if probe is not None:
        report.update({"mode": "boot-key-probe", "key": probe.key,
                       "trigger": probe.marker.decode("ascii"), "tx_events": []})
    with ExitStack() as stack:
        raw = stack.enter_context(raw_path.open("xb"))
        metadata = stack.enter_context(report_path.open("x"))
        fd, previous = None, None
        try:
            # The receive-only CLI never enables the explicit probe path.
            access = os.O_RDONLY if probe is None else os.O_RDWR
            fd = os.open(str(port), access | os.O_NOCTTY | os.O_NONBLOCK)
            previous = termios.tcgetattr(fd)
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL)
            for flag in ("CRTSCTS", "CCTS_OFLOW", "CRTS_IFLOW"):
                attrs[2] &= ~getattr(termios, flag, 0)
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            # Preserve pending output in receive-only mode. A boot-key probe
            # discards stale input before arming so old banners cannot trigger TX.
            if probe is not None:
                termios.tcflush(fd, termios.TCIFLUSH)
                report["stale_input_flushed_before_arming"] = True
            if on_ready:
                on_ready()
            deadline = time.monotonic() + duration
            report["reason"] = "duration-expired"
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if not select.select([fd], [], [], min(remaining, 0.25))[0]:
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
                print(display_bytes(data), end="", flush=True)
                key = probe.observe(data) if probe is not None else b""
                if key:
                    # Exactly one allowlisted byte; never a shell/flash command.
                    if not select.select([], [fd], [], 1.0)[1]:
                        raise TimeoutError("serial key write timed out")
                    written = os.write(fd, key)
                    report["serial_bytes_sent"] += written
                    report["tx_events"].append({"utc": utc_now(), "hex": key[:written].hex(),
                                                "after_rx_bytes": report["bytes_received"]})
                    if written != 1:
                        raise OSError("serial key write was incomplete")
                    print("\n[Sent one %s key after the boot banner]" % probe.key, flush=True)
        except KeyboardInterrupt:
            report["reason"] = "interrupted"
        except Exception as exc:
            report["reason"] = "error"
            report["error"] = str(exc)
            raise
        finally:
            if fd is not None:
                try:
                    if previous is not None:
                        termios.tcsetattr(fd, termios.TCSANOW, previous)
                except termios.error as exc:
                    report["restore_error"] = str(exc)
                finally:
                    os.close(fd)
            report["ended_utc"] = utc_now()
            json.dump(report, metadata, indent=2)
            metadata.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list macOS callout ports and exit")
    parser.add_argument("--port", help="physical serial device, e.g. /dev/cu.PL2303G-USBtoUART3110")
    parser.add_argument("--baud", type=int, default=115200, help="baud rate; 115200 decoded this receiver's boot log")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--label", default="", help="wiring/experiment notes for the report")
    parser.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    if args.list:
        print("\n".join(sorted(glob.glob("/dev/cu.*"))))
        return 0
    if not args.port:
        parser.error("--port is required (use --list to inspect available ports)")
    try:
        report = capture(args.port, args.baud, args.seconds, args.output, args.label,
                         lambda: print("Listening at %d baud, 8N1; no serial data will be sent." % args.baud, flush=True))
    except (OSError, ValueError, termios.error) as exc:
        print("Capture failed: %s" % exc, file=sys.stderr)
        return 2
    print("\nCaptured %d bytes: %s (%s)" % (report["bytes_received"], report["raw_file"], report["reason"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
