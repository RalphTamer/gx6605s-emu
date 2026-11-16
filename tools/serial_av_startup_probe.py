#!/usr/bin/env python3
"""Fixed volatile AV startup experiment, followed by a read-only RAM monitor.

Uploads the two pinned, unmodified native AV programs. Only the observed AV
register writes and bounded DRAM are touched; no flash/OTP/GCTL commands.
CPU IRQ is disabled. Startup polling is bounded. The second image is a warm
reload, not an independent cold experiment. Output includes both full AV RAM
captures with a header/data CRC and then enters the read-only register monitor.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from csky_ram_asm import Assembler
from serial_flash_dump import crc_table
import serial_ram_diagnostic as ram
import serial_ram_probe as stage1
import serial_register_monitor as monitor

MODE = 'fixed-av-startup-with-resident-reader'
SUCCESS = 'av-startup-captured-monitor-ready'
MAGIC = b'STAR-AVBOOT1'.ljust(16, b'\0')
FOOTER = b'END-AVB1'
BASE, SIZE = 0x12633000, 0x90000
AV = 0xa4200000
MONITOR_ENTRY = ram.ENTRY + 0x10000
WINDOWS = monitor.WINDOWS + ((BASE, BASE + SIZE),)
SAVES = (0x11000000, 0x11100000)
IMAGES = (
    ('receiver', 'a41ec193a6665de571332ab23e994fbe9190f0afc54a07dcee62351d80406717',
     0x1263f000, (0x12633000, 0x12651000, 0x12678000, 0, 4, 0, 0x1e000)),
    ('v393', 'f85906d314851fce79231e22b21d573e4e6400187dc9108662a0c7f4dc71cf5e',
     0x12633000, (0x12633000, 0x12636000, 0x1265d000, 0, 4, 0, 0x3000)),
)


def image_data():
    result = []
    for index, (name, digest, destination, config) in enumerate(IMAGES):
        raw = (Path(__file__).resolve().parents[1] / 'analysis' / (name + '-av-full-program.bin')).read_bytes()
        if len(raw) != 0x3000 or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('AV program differs from pinned native ' + name + ' image')
        words = struct.unpack('<6144H', raw)
        packed = b''.join(struct.pack('<II', words[i+2] << 16 | words[i+3], words[i] << 16 | words[i+1])
                          for i in range(0, len(words), 4))
        result.append(struct.pack('<9I', index, destination, *config) + raw + packed)
    return result


def program(sample_size=SIZE, poll_budget=1000000, first_image=0, capture_running_status=False):
    if type(sample_size) is not int or not 4 <= sample_size <= SIZE or sample_size % 4:
        raise ValueError('AV sample size must be aligned and within its reserved DRAM')
    if type(poll_budget) is not int or not 1 <= poll_budget <= 1000000:
        raise ValueError('AV poll budget outside fixed bounds')
    if type(first_image) is not int or first_image not in (0, 1) or type(capture_running_status) is not bool or (first_image and not capture_running_status):
        raise ValueError('alternate first image requires the version-2 running-status capture')
    a = Assembler(ram.ENTRY); e = a.emit
    e('lrw', 3, 0xa0403000); e('lrw', 4, 'header'); e('movi', 5, 16); e('bsr', 'send')
    e('lrw', 10, 'header'); e('mfcr', 6, 0); e('store', 6, 10, 32, 4); e('psrclr', 1)
    e('lrw', 13, AV)
    for index in range(2):
        e('lrw', 12, 'image%d' % (index ^ first_image)); e('lrw', 10, 'record%d' % index)
        e('lrw', 14, SAVES[index] | 0xa0000000); e('bsr', 'boot')
    e('lrw', 9, 'crc-table'); e('lrw', 8, 0xffffffff)
    blocks = [('header', 48), ('record0', 32), (SAVES[0] | 0xa0000000, sample_size),
              ('record1', 32), (SAVES[1] | 0xa0000000, sample_size)]
    for pointer, length in blocks:
        e('lrw', 4, pointer); e('lrw', 5, length); e('bsr', 'crc')
    e('not', 8); e('lrw', 6, 'tail'); e('store', 8, 6, 0, 4)
    e('lrw', 4, 'header'); e('addi', 4, 16); e('movi', 5, 32); e('bsr', 'send')
    for pointer, length in blocks[1:] + [('tail', 12)]:
        e('lrw', 4, pointer); e('lrw', 5, length); e('bsr', 'send')
    e('lrw', 1, MONITOR_ENTRY); e('jmp', 1)

    a.label('boot')
    e('load', 6, 12, 0, 4); e('store', 6, 10, 0, 4)
    e('load', 6, 13, 0x18, 4); e('store', 6, 10, 20, 4)
    e('lrw', 7, AV + 0x160); e('load', 6, 7, 0, 4); e('store', 6, 10, 12, 4)
    e('movi', 6, 0); e('store', 6, 13, 0, 4)  # stop before replacing buffers/code
    e('load', 6, 10, 20, 4); e('cmpnei', 6, 0); e('bf', 'clear-start')
    e('movi', 9, 2); e('movi', 8, 0); e('br', 'record-result')
    a.label('clear-start')
    # Clear the entire fixed AV region, including all native buffer extents.
    e('lrw', 4, BASE | 0xa0000000); e('lrw', 5, SIZE // 4); e('movi', 6, 0)
    a.label('clear'); e('store', 6, 4, 0, 4); e('addi', 4, 4); e('subi', 5, 1)
    e('cmpnei', 5, 0); e('bt', 'clear')
    e('mov', 4, 12); e('lrw', 6, 36 + 0x3000); e('addu', 4, 6)
    e('load', 5, 12, 4, 4); e('lrw', 6, 0xa0000000); e('or', 5, 6); e('lrw', 7, 0x3000 // 4)
    a.label('copy-code'); e('load', 6, 4, 0, 4); e('store', 6, 5, 0, 4)
    e('addi', 4, 4); e('addi', 5, 4); e('subi', 7, 1); e('cmpnei', 7, 0); e('bt', 'copy-code')
    # Native order: buffer 2, buffer 3, buffer 1, then configuration words.
    e('lrw', 7, AV + 0x100)
    for source, target in ((12, 4), (16, 8), (8, 0), (20, 12), (24, 16)):
        e('load', 6, 12, source, 4); e('store', 6, 7, target, 4)
    e('lrw', 7, AV + 0x170); e('load', 6, 12, 28, 4); e('store', 6, 7, 0, 4)
    e('mov', 4, 12); e('lrw', 6, 36); e('addu', 4, 6); e('movi', 5, 0); e('lrw', 7, 2048)
    a.label('upload')
    e('load', 6, 4, 0, 2); e('mov', 8, 5); e('lsli', 8, 16); e('or', 8, 6); e('store', 8, 13, 4, 4)
    e('addi', 4, 2); e('addi', 5, 1); e('cmpne', 5, 7); e('bt', 'upload')
    e('lrw', 7, AV + 0x160); e('movi', 6, 1); e('store', 6, 7, 0, 4)
    e('lrw', 7, AV + 0x58); e('load', 6, 12, 32, 4); e('store', 6, 7, 0, 4)
    e('movi', 6, 1); e('store', 6, 13, 0, 4)
    e('lrw', 7, AV + 0x160); e('lrw', 5, poll_budget); e('movi', 8, 0)
    a.label('poll'); e('load', 6, 7, 0, 4); e('addi', 8, 1); e('cmpnei', 6, 0); e('bf', 'ready')
    e('subi', 5, 1); e('cmpnei', 5, 0); e('bt', 'poll')
    e('movi', 9, 1); e('br', 'record-result')
    a.label('ready'); e('movi', 9, 0)
    a.label('record-result')
    e('store', 9, 10, 4, 4); e('store', 8, 10, 8, 4)
    e('lrw', 7, AV + 0x160); e('load', 6, 7, 0, 4); e('store', 6, 10, 16, 4)
    for offset, output in ((0x58, 24), (0x170, 28)):
        e('lrw', 7, AV + offset); e('load', 6, 7, 0, 4); e('store', 6, 10, output, 4)
    if capture_running_status:
        # Additional reads only. Preserve the live +18 value before stopping;
        # v1 sampled this register only before each startup and after the stop.
        e('load', 6, 13, 0x18, 4); e('load', 7, 12, 0, 4); e('lsli', 7, 2)
        e('lrw', 4, 'header'); e('addu', 4, 7); e('store', 6, 4, 36, 4)
    e('movi', 6, 0); e('store', 6, 13, 0, 4)
    e('lrw', 4, BASE | 0xa0000000); e('mov', 5, 14); e('lrw', 7, sample_size // 4)
    a.label('snapshot'); e('load', 6, 4, 0, 4); e('store', 6, 5, 0, 4)
    e('addi', 4, 4); e('addi', 5, 4); e('subi', 7, 1); e('cmpnei', 7, 0); e('bt', 'snapshot'); e('jmp', 15)

    a.label('crc')
    e('load', 6, 4, 0, 1); e('xor', 8, 6); e('mov', 7, 8); e('zextb', 7); e('lsli', 7, 2)
    e('addu', 7, 9); e('load', 7, 7, 0, 4); e('lsri', 8, 8); e('xor', 8, 7)
    e('addi', 4, 1); e('subi', 5, 1); e('cmpnei', 5, 0); e('bt', 'crc'); e('jmp', 15)
    a.label('send')
    e('load', 6, 3, 20, 4); e('btsti', 6, 6); e('bf', 'send')
    e('load', 2, 4, 0, 1); e('store', 2, 3, 0, 4); e('addi', 4, 1); e('subi', 5, 1)
    e('cmpnei', 5, 0); e('bt', 'send'); e('jmp', 15)
    images = image_data()
    code = a.finish({'header': MAGIC + struct.pack('<8I', 2 if capture_running_status else 1, 2, BASE, sample_size, 0, 0, 0, first_image),
                     'record0': bytes(32), 'record1': bytes(32), 'tail': bytes(4) + FOOTER,
                     'image0': images[0], 'image1': images[1], 'crc-table': crc_table()})
    if len(code) > MONITOR_ENTRY - ram.ENTRY:raise ValueError('AV probe overlaps resident monitor')
    reader, waiting = monitor.program(base=MONITOR_ENTRY, windows=WINDOWS)
    return code.ljust(MONITOR_ENTRY - ram.ENTRY, b'\0') + reader, waiting


def parse_reply(data, sample_size=SIZE):
    body_size = 48 + 2 * (32 + sample_size)
    total = 1 + body_size + 12 + len(monitor.MAGIC)
    if len(data) < total:return None
    if len(data) != total or data[:17] != b'O' + MAGIC or data[-16:] != monitor.MAGIC:
        raise ValueError('AV trace framing/monitor handoff mismatch')
    body = memoryview(data)[1:1+body_size]
    if bytes(data[1+body_size+4:1+body_size+12]) != FOOTER:
        raise ValueError('AV trace footer mismatch')
    if zlib.crc32(body) != struct.unpack_from('<I', data, 1+body_size)[0]:
        raise ValueError('AV trace header/data CRC mismatch')
    version, count, base, amount, psr, *reserved = struct.unpack_from('<8I', body, 16)
    if (count, base, amount) != (2, BASE, sample_size) or version not in (1, 2) or (version == 1 and any(reserved)) or (version == 2 and reserved[2] not in (0, 1)):
        raise ValueError('AV trace metadata mismatch')
    first_image = reserved[2] if version == 2 else 0
    results = []
    for index in range(2):
        offset = 48 + index * (32 + sample_size)
        ident, status, polls, before, after, boot, config58, config170 = struct.unpack_from('<8I', body, offset)
        if ident != (index ^ first_image) or status not in (0, 1, 2) or (status == 0 and (after != 0 or polls == 0)):
            raise ValueError('AV trace run metadata mismatch')
        raw = body[offset+32:offset+32+sample_size]
        results.append({'image': IMAGES[ident][0], 'status': ('mailbox-zero', 'poll-timeout', 'unexpected-boot-status')[status],
                        'polls': polls, 'mailbox_before': hex(before), 'mailbox_after': hex(after),
                        'initial_boot_status': hex(boot), 'configuration_58': hex(config58), 'configuration_170': hex(config170),
                        'ram_sha256': hashlib.sha256(raw).hexdigest(), 'ram_size': sample_size,
                        'context': 'first startup after RAM bootstrap' if index == 0 else 'warm stop/reload of second native image'})
        if version == 2:results[-1]['pre_stop_status_18'] = hex(reserved[ident])
    return {'version': version, 'first_image': first_image, 'runs': results, 'initial_psr': hex(psr), 'monitor_read_only': True,
            'limitations': 'Fixed volatile AV writes and sequential stopped RAM snapshots. Second image is a warm reload; neither establishes an instruction-set decoder.'}


class ProbeBus(ram.DiagnosticBus):
    """Offline transport fixture, not a prediction of physical MCU execution."""
    def __init__(self, stalled=False, first_image=0):
        super().__init__()
        self.registers = {0x18: 0, 0x160: 0x94d94ace, 0x58: 0, 0x170: 0}
        self.words = {}; self.starts = 0; self.stalled = stalled
        self.order = (first_image, first_image ^ 1)

    def access(self, address, size, value=None, fetch=False):
        from starsat_emu.av_mcu import AVMicrocontroller
        from starsat_emu.video import ram_bytes
        if not fetch and 0x04200000 <= address < 0x04200174:
            offset = address - 0x04200000
            if size != 4 or offset not in AVMicrocontroller.offsets:
                raise ValueError('unexpected AV diagnostic register access')
            if value is None:
                if offset not in (0x18, 0x160, 0x58, 0x170):raise ValueError('unexpected AV diagnostic readback')
                if offset == 0x18 and self.registers.get(0) == 1:return 0x8b
                return self.registers[offset]
            self.device_writes.append((address, value, size))
            if offset == 4:
                index = value >> 16
                if index >= 2048:raise ValueError('instruction upload outside bounded IRAM')
                self.words[index] = value & 0xffff
            elif offset == 0:
                if value not in (0, 1):raise ValueError('unexpected AV control command')
                if value == 1:
                    if self.starts >= 2:raise ValueError('too many AV starts')
                    index = self.order[self.starts]; self.starts += 1
                    image = image_data()[index]
                    assert struct.pack('<2048H', *(self.words[i] for i in range(2048))) == image[36:36+4096]
                    config = IMAGES[index][3]
                    assert [self.registers[o] for o in (0x100, 0x104, 0x108, 0x10c, 0x110, 0x170, 0x58)] == list(config)
                    assert ram_bytes(self, IMAGES[index][2], 0x3000) == image[36+0x3000:]
                    self.registers[0x160] = 1 if self.stalled else 0
                    if not self.stalled:
                        self.write(BASE + 4 * index, 0xaabbcc00 | index)
                self.registers[offset] = value
            else:
                if offset not in (0x100, 0x104, 0x108, 0x10c, 0x110, 0x170, 0x58, 0x160):
                    raise ValueError('unexpected AV diagnostic write')
                self.registers[offset] = value
            return None
        return super().access(address, size, value, fetch)


def check_probe(loader, first_image=0, capture_running_status=False):
    code, waiting = program(sample_size=64, poll_budget=2, first_image=first_image, capture_running_status=capture_running_status)
    bus = ProbeBus(first_image=first_image); cpu = ram.simulate_continuation(loader, code, waiting, bus, limit=10000000)
    result = parse_reply(bytes(bus.outgoing), sample_size=64)
    if bus.starts != 2 or any(run['status'] != 'mailbox-zero' for run in result['runs']):
        raise ValueError('offline fixed AV startup/monitor trace failed')
    allowed = {0x04200000 + o for o in (0, 4, 0x58, 0x100, 0x104, 0x108, 0x10c, 0x110, 0x160, 0x170)} | {0x403000}
    if any(a not in allowed and (a, v, z) != (0xf80004, 1, 4) for a, v, z in bus.device_writes):
        raise ValueError('unexpected diagnostic MMIO write')
    return {'steps': cpu.steps, 'fixture_result': result,
            'physical_packet_sha256': hashlib.sha256(ram.wrap_stage2(program(first_image=first_image, capture_running_status=capture_running_status)[0])).hexdigest(),
            'limitations': 'Fixture validates pinned stage-1, exact native code/data uploads, bounded capture/CRC and reader handoff. MCU outcomes are synthetic fixture inputs, not physical measurements.'}


def validate_monitor_report(ready, raw):
    parsed = parse_reply(raw)
    return (ready.get('mode') == MODE and ready.get('reason') == SUCCESS and
            parsed is not None and ready.get('packet_sha256') == hashlib.sha256(ram.wrap_stage2(
                program(first_image=parsed['first_image'], capture_running_status=parsed['version'] == 2)[0])).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True); parser.add_argument('--loader', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=7200)
    parser.add_argument('--first-image', choices=('receiver', 'v393'), default='receiver')
    parser.add_argument('--running-status', action='store_true', help='version-2 capture also reads +18 before stopping the core')
    parser.add_argument('--output', type=Path, default=Path('traces') / datetime.now(timezone.utc).strftime('serial-av-startup-%Y%m%dT%H%M%S%fZ'))
    args = parser.parse_args()
    print('Checking fixed AV startup trace locally...', flush=True)
    first = ('receiver', 'v393').index(args.first_image)
    packet = ram.wrap_stage2(program(first_image=first, capture_running_status=args.running_status)[0])
    offline = check_probe(args.loader.read_bytes(), first, args.running_status)
    prefix = Path(str(args.output) + '-stage1')
    boot = stage1.probe(args.port, args.loader, args.seconds, prefix)
    if boot['reason'] != 'stage1-ready':return 3
    report = ram.run(args.port, Path(str(prefix) + '.json'), args.loader, args.output,
        packet_factory=lambda: packet, verifier=lambda loader: offline,
        reply_parser=parse_reply, mode=MODE, seconds=600, result_key='av_startup', success_reason=SUCCESS)
    if report['reason'] != SUCCESS:return 3
    raw = Path(str(args.output) + '.bin').read_bytes()
    for index, result in enumerate(report['av_startup']['runs']):
        offset = 1 + 48 + index * (32 + SIZE) + 32
        Path(str(args.output) + '.' + result['image'] + '.ram.bin').write_bytes(raw[offset:offset+SIZE])
    print('AV traces verified; read-only monitor remains in RAM. Keep receiver powered on.', flush=True)
    return 0


if __name__ == '__main__':raise SystemExit(main())
