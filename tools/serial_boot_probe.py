#!/usr/bin/env python3
"""Send one logged control key after a new receiver boot banner, then listen.

Requires the checked full wiring: receiver yellow to UGREEN 20201 terminal 5,
green to terminal 3, orange to terminal 2. Start with receiver power off and
multimeter probes removed. No firmware download, erase or flash command is sent.
Whether any key interrupts this installed bootloader is not yet established.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import termios

from serial_capture import BootKeyProbe, capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--key", choices=BootKeyProbe.KEYS, default="escape")
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--output", type=Path, default=Path("traces") / datetime.now(timezone.utc).strftime("serial-probe-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    try:
        report = capture(args.port, 115200, args.seconds, args.output,
                         "green RX3; yellow GND5; orange TX2; receiver input divider measured -3 V at idle",
                         lambda: print("ARMED at 115200 baud: power on receiver. One %s key only after Partition Version appears." % args.key, flush=True),
                         BootKeyProbe(args.key))
    except (OSError, ValueError, termios.error) as exc:
        print("Probe failed: %s" % exc, file=sys.stderr)
        return 2
    print("\nReceived %d bytes; sent %d byte(s). Saved %s (%s)" % (
        report["bytes_received"], report["serial_bytes_sent"], report["raw_file"], report["reason"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
