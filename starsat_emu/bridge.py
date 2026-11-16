"""JSON-lines subprocess interface for a desktop debugger. No network listener."""
import json
from pathlib import Path
import pickle
import queue
import sys
import threading
import time
from .accelerator import Accelerator
from .profile import MachineProfile
from .session import Session
from .memory import EmulationStop
from .video import OSDRenderer

BOOT_CACHE_DIR = Path("/tmp/starsat_boot_cache")


def _cache_path(session):
    sha = session.firmware_info["sha256"][:16]
    prof = session.profile.name.replace("/", "_")
    return BOOT_CACHE_DIR / f"{sha}_{prof}.pkl"


def _save_boot_cache(session):
    BOOT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bus = session.cpu.bus
    acc = session.accelerator
    saved = {}
    for attr in ("timers", "rtc", "ehci", "ohci", "video"):
        dev = getattr(bus, attr, None)
        if dev is not None and hasattr(dev, "clock"):
            saved[attr] = dev.clock
            dev.clock = None
    if bus.ohci:
        for idx, ctrl in enumerate(bus.ohci.controllers):
            saved[("ohci", idx)] = ctrl.clock
            ctrl.clock = None
    g_ref = bus.graphics.bus_ref if bus.graphics else None
    try:
        session.accelerator = None
        if bus.graphics:
            bus.graphics.bus_ref = None
        tmp = _cache_path(session).with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(session, f)
        tmp.replace(_cache_path(session))
    finally:
        session.accelerator = acc
        for attr, clk in saved.items():
            if isinstance(attr, tuple):
                bus.ohci.controllers[attr[1]].clock = clk
            else:
                getattr(bus, attr).clock = clk
        if bus.graphics:
            bus.graphics.bus_ref = g_ref


def _load_boot_cache(session):
    path = _cache_path(session)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        loaded = pickle.load(f)
    bus = loaded.cpu.bus
    for attr in ("timers", "rtc", "ehci", "ohci", "video"):
        dev = getattr(bus, attr, None)
        if dev is not None and hasattr(dev, "clock"):
            dev.clock = lambda b=bus: b.ticks
    if bus.ohci:
        for ctrl in bus.ohci.controllers:
            ctrl.clock = lambda b=bus: b.ticks
    if bus.graphics:
        bus.graphics.bus_ref = lambda b=bus: b
    loaded.video_renderer = OSDRenderer()
    loaded.accelerator = Accelerator(loaded.cpu)
    loaded.stop = None
    return loaded


def serve(firmware, profile, input_stream=None, output_stream=None):
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    requests = queue.Queue()

    def reader():
        for line in input_stream:
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("request must be an object")
                requests.put(value)
            except (ValueError, TypeError) as error:
                requests.put({"command": "invalid", "error": str(error)})
        requests.put(None)

    def emit(event, **data):
        output_stream.write(json.dumps({"event": event, **data}) + "\n")
        output_stream.flush()

    session = Session(firmware, profile)
    remaining = 0
    breakpoints = set()
    last_update = 0
    last_frame = None
    cache_saved = False

    def state_event():
        nonlocal last_frame
        state = session.snapshot(include_frame=True)
        display = state["display"]
        frame_id = display.get("id")
        if frame_id is not None and frame_id == last_frame:
            display.pop("png_base64", None)
        last_frame = frame_id
        emit("state", running=remaining != 0, state=state)

    threading.Thread(target=reader, daemon=True).start()
    state_event()
    while True:
        request = ...
        try:
            request = requests.get(timeout=0 if remaining != 0 else 0.1)
        except queue.Empty:
            pass
        if request is None:
            return 0
        if request is not ...:
            try:
                command = request.get("command")
                if command == "quit":
                    return 0
                elif command == "run":
                    count = request.get("count")
                    if count is not None and (type(count) is not int or not 1 <= count <= 100000000):
                        raise ValueError("run count must be 1..100000000")
                    remaining = count
                elif command == "fast_boot":
                    cached = _load_boot_cache(session)
                    if cached is not None:
                        session = cached
                        last_frame = None
                        cache_saved = True
                        remaining = None
                    else:
                        remaining = None
                elif command == "pause":
                    remaining = 0
                elif command == "step":
                    count = request.get("count", 1)
                    if type(count) is not int or not 1 <= count <= 10000:
                        raise ValueError("step count must be 1..10000")
                    remaining = 0
                    session.advance(count)
                elif command == "reset":
                    new_profile = MachineProfile.load(request.get("profile", "exploratory"))
                    session = Session(firmware, new_profile)
                    cache_saved = False
                    last_frame = None
                    remaining = 0
                elif command == "input":
                    text = request.get("text", "")
                    if not isinstance(text, str) or len(text) > 4096:
                        raise ValueError("UART input must be text up to 4096 characters")
                    session.cpu.bus.uart.rx.extend(text.encode())
                    if text and session.stop and session.stop["reason"] == "waiting-for-uart":
                        remaining = None
                elif command == "remote":
                    code = request.get("code")
                    if isinstance(code, str):
                        code = int(code, 0)
                    if type(code) is not int or not 0 <= code <= 0xffffffff:
                        raise ValueError("remote code must be a 32-bit integer")
                    if session.cpu.bus.infrared is None:
                        raise ValueError("infrared receiver is not enabled in this profile")
                    session.cpu.bus.infrared.queue_nec(code)
                    if remaining == 0:
                        remaining = 25000000
                elif command == "memory":
                    raw_address = request.get("address", "0x100000")
                    address = int(raw_address, 0) if isinstance(raw_address, str) else raw_address
                    size = request.get("size", 64)
                    if type(address) is not int or type(size) is not int:
                        raise ValueError("memory address and size must be integers")
                    emit("memory", address=hex(address), data=session.memory(address, size).hex())
                elif command == "breakpoint":
                    raw = request.get("address")
                    value = int(raw, 0) if isinstance(raw, str) else raw
                    if type(value) is not int or not 0 <= value <= 0xffffffff:
                        raise ValueError("breakpoint requires a 32-bit address")
                    if request.get("remove", False):
                        breakpoints.discard(value)
                    else:
                        breakpoints.add(value)
                elif command != "state":
                    raise ValueError(request.get("error", "unknown command"))
                state_event()
            except (ValueError, OSError, EmulationStop) as error:
                emit("error", message=str(error))
        if remaining != 0:
            count = 250000 if remaining is None else min(250000, remaining)
            before = session.cpu.steps
            stop = session.advance(count, breakpoints)
            if remaining is not None:
                remaining -= session.cpu.steps - before
            if not cache_saved and session.cpu.steps >= 2950000000 and session.profile.name == "gx6605s-receiver-runtime":
                try:
                    _save_boot_cache(session)
                    cache_saved = True
                except OSError:
                    pass
            if stop:
                remaining = 0
            elif remaining is not None and remaining <= 0:
                remaining = 0
                session.stop = {"reason": "instruction-budget", "detail": "requested run budget reached"}
            if remaining == 0 or time.monotonic() - last_update >= 0.2:
                state_event()
                last_update = time.monotonic()
