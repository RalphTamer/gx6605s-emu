"""python3 -m starsat_emu --help"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys

from .cpu import decode
from .firmware import Firmware
from .memory import EmulationStop


def integer(text):
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer, optionally prefixed with 0x")


def positive(text):
    value = integer(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def parser():
    p = argparse.ArgumentParser(description="Experimental C-SKY ABIv1 StarSat boot-code emulator; does not boot the menus yet.")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("inspect", "disasm", "run", "bridge"):
        child = sub.add_parser(command)
        child.add_argument("firmware", type=Path)
        child.add_argument("--offset", type=integer, help="payload file offset (default: detect USB header or raw flash)")
        if command == "bridge":
            child.add_argument("--profile", default="exploratory")
        if command in ("run", "disasm"):
            child.add_argument("--base", type=integer, default=0x100000, help="boot SRAM physical base")
            child.add_argument("--load-size", type=positive, default=0x3000, help="bytes preloaded by the provisional BootROM shim")
        if command == "disasm":
            child.add_argument("--start", type=integer, default=0x100000, help="physical address")
            child.add_argument("--count", type=positive, default=24)
        if command == "run":
            child.add_argument("--entry", type=integer, help="default: base")
            child.add_argument("--max-steps", type=positive, default=5000000)
            child.add_argument("--profile", default="strict", help="strict, exploratory, or a JSON profile path")
            child.add_argument("--trace-from", type=integer, default=0, help="start tracing at this instruction count")
            child.add_argument("--trace-limit", type=integer, default=20000, help="maximum trace records; 0 for unlimited")
            child.add_argument("--breakpoint", type=integer, action="append", default=[])
            child.add_argument("--trace", type=Path, help="write JSONL instruction trace")
            child.add_argument("--report", type=Path, help="write final JSON state")
            child.add_argument("--uart-input", default="", help="UTF-8 text queued in the polling UART")
    return p


def checked_outputs(source, paths):
    paths = [p for p in paths if p is not None]
    resolved = [p.resolve() for p in paths]
    if source.resolve() in resolved or len(set(resolved)) != len(resolved):
        raise ValueError("output paths must differ from the firmware and from one another")
    for path in paths:
        if path.exists() and path.samefile(source):
            raise ValueError("output path points to the firmware")
    for i, path in enumerate(paths):
        for other in paths[i + 1:]:
            if path.exists() and other.exists() and path.samefile(other):
                raise ValueError("output paths point to the same file")
    return paths


def run(args, fw):
    from .session import Session
    from .profile import MachineProfile
    checked_outputs(args.firmware, [args.trace, args.report])
    if args.trace_from < 0 or args.trace_limit < 0:
        raise ValueError("trace limits cannot be negative")
    profile = MachineProfile.load(args.profile)
    session = Session(fw, profile, args.base, args.load_size, args.entry)
    session.cpu.bus.uart.rx.extend(args.uart_input.encode())
    print(f"Profile: {profile.name}. Experimental partial emulation; receiver startup is incomplete.")
    if profile.assumptions:
        print("Device reset values and completion timing include explicit assumptions; see report/profile.")
    written = 0
    with ExitStack() as stack:
        trace_file = None
        if args.trace:
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            trace_file = stack.enter_context(args.trace.open("w"))
        def trace(row):
            nonlocal written
            trace_file.write(json.dumps(row) + "\n")
            written += 1
        try:
            while session.cpu.steps < args.max_steps:
                tracing = trace_file is not None and session.cpu.steps >= args.trace_from and (not args.trace_limit or written < args.trace_limit)
                count = min(10000, args.max_steps - session.cpu.steps)
                if trace_file is not None and session.cpu.steps < args.trace_from:
                    count = min(count, args.trace_from - session.cpu.steps)
                if tracing and args.trace_limit:
                    count = min(count, args.trace_limit - written)
                if session.advance(count, args.breakpoint, trace if tracing else None):
                    break
            else:
                session.stop = {"reason": "instruction-budget", "detail": "instruction limit reached"}
        except KeyboardInterrupt:
            session.stop = {"reason": "interrupted", "detail": "execution interrupted by user"}
        report = session.snapshot()
        report["trace"] = {"records": written, "from_step": args.trace_from, "limit": args.trace_limit}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Executed {session.cpu.steps} instructions; pc=0x{session.cpu.pc:08x}")
        print(f"Stopped: {report['reason']}: {report['detail']}")
        if session.cpu.bus.uart.tx:
            print("UART:", report["uart_output_text"])
        if report["spi"]:
            print(f"SPI flash: {report['spi']['flash_read_bytes']} bytes read in {report['spi']['transfers']} transfers")
        for row in report["recent_instructions"][-8:]:
            print(f"  {row['pc']}  {row['word']}  {row['instruction']}")
        return 0 if report["reason"] in ("halt", "breakpoint") else 3 if report["reason"] == "instruction-budget" else 2


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        fw = Firmware(args.firmware, args.offset)
        if args.command == "bridge":
            from .bridge import serve
            from .profile import MachineProfile
            return serve(fw, MachineProfile.load(args.profile))
        if args.command == "inspect":
            print(json.dumps(fw.inspect(), indent=2))
            return 0
        if args.command == "disasm":
            cpu = fw.machine(args.base, args.load_size)
            pc = args.start
            for _ in range(args.count):
                word = cpu.bus.read(pc, 2, fetch=True)
                print(f"{pc:08x}  {word:04x}  {decode(word, pc)}")
                pc += 2
            return 0
        return run(args, fw)
    except (OSError, ValueError, EmulationStop) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
