#!/usr/bin/env python3
"""Offline hypotheses for this exact firmware's unresolved VPU readback.

This is a diagnostic experiment. It does not change a firmware file or the
default machine profile, and a plausible size alone never counts as a boot.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from starsat_emu.accelerator import Accelerator
from starsat_emu.firmware import Firmware
from starsat_emu.profile import MachineProfile
from starsat_emu.session import Session
from starsat_emu.__main__ import checked_outputs

IMAGE_HASH = "10c0962a5f1fef6a5d0313e5e795dd376b9148cc4221696db9c2e99471b4d5b3"
BEFORE_INPUT = 0x93fbb58c
AFTER_PASSES = 0x93fb1730
BEFORE_ROMFS = 0x93fb1746
WINDOW = range(0x140, 0x1c0, 4)


def fork_session(session):
    """Copy RAM/device state without ctypes pointers or a clock closure."""
    accelerator, clock = session.accelerator, session.cpu.bus.rtc.clock
    session.accelerator = session.cpu.bus.rtc.clock = None
    try:
        result = copy.deepcopy(session)
    finally:
        session.accelerator, session.cpu.bus.rtc.clock = accelerator, clock
    result.accelerator = Accelerator(result.cpu)
    bus = weakref.ref(result.cpu.bus)
    result.cpu.bus.rtc.clock = lambda: bus().ticks
    return result


def advance_to(session, address, budget):
    end = session.cpu.steps + budget
    while session.cpu.steps < end:
        stop = session.advance(min(5000000, end - session.cpu.steps), [address])
        if stop:
            return stop
    return {"reason": "instruction-budget", "detail": "probe budget reached"}


def valid_romfs(data):
    """Check the standard ROMFS superblock, bounds, name and checksum."""
    if len(data) < 32 or data[:8] != b"-rom1fs-":
        return False
    size = int.from_bytes(data[8:12], "big")
    if not 32 <= size <= len(data) or size % 16:
        return False
    end = data.find(b"\0", 16, min(size, 512))
    if end < 0 or ((end + 16) & ~15) + 16 > size:
        return False
    block = data[:min(size, 512)]
    return sum(struct.unpack(">" + "I" * (len(block) // 4), block)) & 0xffffffff == 0


def default_candidates(written):
    result = []
    for name, value, preserve in [("all-zero", 0, False), ("all-one", 0xffffffff, False),
                                  ("written-and-zero", 0, True), ("written-and-one", 0xffffffff, True)]:
        result.append({"name": name, "registers": {offset: written.get(offset, value) if preserve else value for offset in WINDOW}})
    for name, mask in [("byte-masks", 0x00ff00ff), ("nibble-masks", 0x0f0f0f0f),
                       ("low-sixteen", 0xffff), ("low-twenty-four", 0xffffff)]:
        result.append({"name": name, "registers": {offset: written.get(offset, 0) & mask for offset in WINDOW}})
    result.append({"name": "first-word-mirrored", "registers": {offset: written[0x140] for offset in WINDOW}})
    return result


def load_candidates(path):
    raw = json.loads(path.read_text())
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise ValueError("candidate file must contain 1..64 objects")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "registers"} or not isinstance(item["name"], str) or not isinstance(item["registers"], dict):
            raise ValueError("each candidate requires a name and a registers object")
        registers = {}
        for address, value in item["registers"].items():
            address = int(address, 0)
            value = int(value, 0) if isinstance(value, str) else value
            if address - 0x04800000 not in WINDOW or type(value) is not int or not 0 <= value <= 0xffffffff:
                raise ValueError("candidate requires aligned physical addresses in 0x04800140..0x048001BC and 32-bit values")
            registers[address - 0x04800000] = value
        if set(registers) != set(WINDOW):
            raise ValueError("each candidate must specify all 32 register words")
        result.append({"name": item["name"], "registers": registers})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--candidates", type=Path, help="JSON candidates with all 32 physical register words; default: nine explicit hypotheses")
    parser.add_argument("--report", type=Path, default=Path("traces/offline-decoder-probe.json"))
    parser.add_argument("--romfs-max-steps", type=int, default=2000000000,
                        help="instruction budget after the size check; default 2 billion")
    parser.add_argument("--decoded-output", type=Path,
                        help="save the first checksum-valid decoded ROMFS to a new file")
    args = parser.parse_args(argv)
    if not 1 <= args.romfs_max_steps <= 10000000000:
        raise ValueError("ROMFS instruction budget must be within 1..10 billion")
    checked_outputs(args.firmware, [args.report, args.decoded_output])
    if args.decoded_output and args.decoded_output.exists():
        raise FileExistsError("decoded output already exists")
    if args.candidates:
        checked_outputs(args.candidates, [args.report, args.decoded_output])
    firmware = Firmware(args.firmware)
    if hashlib.sha256(firmware.data).hexdigest() != IMAGE_HASH:
        raise ValueError("probe addresses are verified only for the original SR-4040HD VEGA V3.93 image")
    profile = MachineProfile.load("exploratory")
    session = Session(firmware, profile)
    if session.accelerator.native is None:
        raise ValueError("build the native accelerator first: sh tools/build_macos.sh")
    print("Running firmware to the register-dependent decoder…", flush=True)
    stop = advance_to(session, BEFORE_INPUT, 100000000)
    if stop["reason"] != "breakpoint":
        raise ValueError(f"could not reach probe boundary: {stop}")
    candidates = load_candidates(args.candidates) if args.candidates else default_candidates(session.cpu.bus.video.registers)
    report = {"firmware_sha256": IMAGE_HASH, "diagnostic_only": True,
              "warning": "Candidate readback semantics require validation even when some values were measured. Size checks alone do not establish valid decoding or receiver boot.",
              "romfs_instruction_budget": args.romfs_max_steps,
              "checkpoint_steps": session.cpu.steps, "results": []}
    exported = False
    args.report.parent.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        started = time.monotonic()
        attempt = fork_session(session)
        attempt.cpu.bus.video.registers.update(candidate["registers"])
        stop = advance_to(attempt, AFTER_PASSES, 1600000000)
        result = {"name": candidate["name"], "registers": {hex(0x04800000 + k): hex(v) for k, v in candidate["registers"].items()},
                  "stop": stop, "valid_romfs": False, "romfs_validation": "not-reached"}
        if stop["reason"] == "breakpoint":
            address = attempt.cpu.regs[13]
            header = attempt.memory(address, 32)
            size = int.from_bytes(header[:4], "little")
            result.update({"decoded_head": header.hex(), "decoded_size": size,
                           "size_check_passed": size <= 0x2fffff})
            if size <= 0x2fffff:
                next_stop = advance_to(attempt, BEFORE_ROMFS, args.romfs_max_steps)
                result["romfs_stop"] = next_stop
                result["romfs_validation"] = next_stop["reason"]
                if next_stop["reason"] == "breakpoint":
                    address = attempt.cpu.regs[13] & 0x1fffffff
                    for region in attempt.cpu.bus.regions:
                        offset = address - region.base
                        if 0 <= offset and offset + size <= len(region.data):
                            result["valid_romfs"] = valid_romfs(bytes(region.data[offset:offset + size]))
                            result["romfs_validation"] = "valid" if result["valid_romfs"] else "invalid-superblock-or-checksum"
                            if result["valid_romfs"] and args.decoded_output and not exported:
                                args.decoded_output.parent.mkdir(parents=True, exist_ok=True)
                                with args.decoded_output.open("xb") as out:
                                    out.write(region.data[offset:offset + size])
                                result["decoded_output"] = str(args.decoded_output)
                                exported = True
                            break
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["results"].append(result)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{result['name']}: size={result.get('decoded_size', 'unavailable')}; valid ROMFS={result['valid_romfs']}", flush=True)
    print(f"Report: {args.report}", flush=True)
    return 0 if any(r["valid_romfs"] for r in report["results"]) else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(2)
