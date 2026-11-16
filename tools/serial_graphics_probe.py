#!/usr/bin/env python3
"""Fixed RAM-only GA experiment plus read-only flash factory ID capture.

Loads one 80-byte native-style job, copying a 4x3 pixel rectangle with row
padding between fixed scratch DRAM buffers. CPU initialization uses uncached
aliases. Source alpha varies to distinguish a copy from blending. The program
captures guards/padding, controller state and IRQ62 before/after acknowledge.
It restores the interrupt gate and disables the GA engine before reporting.
Unknown hardware behavior is reported, never treated as a successful copy.
The only flash opcodes are 4BH (factory ID) and 48H (three security-bank reads).
No flash programming, erase,
write-enable, OTP-write, arbitrary target write or execute command is provided.
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
from starsat_emu.graphics import GraphicsAccelerator

OLD_MAGIC = b'STAR-GA1'.ljust(16, b'\0')
MAGIC = b'STAR-GA2'.ljust(16, b'\0')
FOOTER = b'END-GA1\0'
HEADER_SIZE, GUARD_SIZE, DATA_SIZE = 96, 160, 3280
SRC, DST, CMD = 0x10200020, 0x10400020, 0x10100000
SOURCE = (0xffff0000, 0x8000ff00, 0x000000ff, 0x40808080,
          0xdeadface, 0xdeadface, 0xdeadface, 0xdeadface) * 3
INITIAL = (0x13579bdf,) * 8 + ((0xff102030,) * 4 + (0x2468ace0,) * 4) * 3 + (0x13579bdf,) * 8
COMMAND = (0x90400707, 0, 0xbbc29800, 0xbbc29807, 0xf2e60a00,
           0, 0, 0, 0, 0, 0, 0, SRC, DST, DST,
           (8 << 18) | (4 << 4), (3 << 18) | (8 << 4),
           (4 << 18) | (3 << 4), (8 << 18) | (4 << 4) | 1, 3 << 18)


def program(poll_budget=1000000):
    if type(poll_budget) is not int or not 1 <= poll_budget <= 1000000:
        raise ValueError('invalid bounded GA polling budget')
    a = Assembler(ram.ENTRY); e = a.emit
    e('lrw', 3, 0xa0403000); e('lrw', 10, 'reply')
    e('mov', 4, 10); e('movi', 5, 16); e('bsr', 'send')
    for label, target, size in [('source', SRC | 0xa0000000, len(SOURCE)*4),
                                 ('initial', (DST-32) | 0xa0000000, GUARD_SIZE),
                                 ('job', CMD | 0xa0000000, 80)]:
        e('lrw', 4, label);e('lrw', 8, target);e('lrw', 5, size);e('bsr', 'copy')
    e('lrw', 13, 0xa4700000); e('lrw', 12, 0xa0500044)
    for off, dest in [(0x10,32),(0x18,36),(0x1c,40)]:
        e('load',6,13,off,4);e('store',6,10,dest,4)
    e('load',11,12,0,4);e('store',11,10,44,4)
    e('lrw',9,'reply-route');e('lrw',8,0xa050009c)
    e('load',7,8,0,4);e('store',7,9,0,4)
    e('lrw',6,0xff00ffff);e('and',7,6);e('lrw',6,0x003e0000);e('or',7,6)
    e('store',7,8,0,4);e('store',7,9,4,4)
    e('lrw',8,0xa0500054);e('load',7,8,0,4);e('store',7,9,8,4)
    e('lrw',6,0xbfffffff);e('and',7,6);e('store',7,8,0,4);e('store',7,9,12,4)
    e('mov',6,11);e('bseti',6,30);e('store',6,12,0,4)
    for off, value in [(0x10,0),(0x1c,0x103),(0x1c,0x101),(4,CMD),(8,80),
                       (0x14,0x977053),(0x24,0xff00ff00),(0x28,0xff00ff00),
                       (0x10,1),(0x18,1),(0x18,0x10001)]:
        e('lrw',6,value);e('store',6,13,off,4)
    e('lrw',14,poll_budget);e('lrw',8,0xa0500004)
    a.label('poll')
    e('load',6,8,0,4);e('btsti',6,30);e('bt','finished')
    e('subi',14,1);e('cmpnei',14,0);e('bt','poll')
    e('movi',6,1);e('store',6,10,20,4)
    a.label('finished')
    for off,dest in [(0x10,48),(0x18,52),(0x1c,56)]:
        e('load',6,13,off,4);e('store',6,10,dest,4)
    e('lrw',8,0xa0500014);e('load',6,8,0,4);e('store',6,10,60,4)
    e('mov',9,10);e('addi',9,32);e('addi',9,32)
    e('lrw',6,0x103);e('store',6,13,0x1c,4)
    e('load',6,13,0x1c,4);e('store',6,9,0,4)
    e('load',6,8,0,4);e('store',6,9,4,4)
    e('store',14,9,8,4)
    e('movi',6,0);e('store',6,13,0x10,4)
    e('lrw',9,'reply-route')
    e('lrw',8,0xa0500000);e('load',7,8,0,4);e('store',7,9,16,4)
    e('load',7,8,4,4);e('store',7,9,20,4)
    e('lrw',8,0xa050009c);e('load',7,9,0,4);e('store',7,8,0,4)
    e('load',7,8,0,4);e('store',7,9,24,4)
    e('lrw',8,0xa0500054);e('load',7,9,8,4);e('store',7,8,0,4)
    e('load',7,8,0,4);e('store',7,9,28,4)
    e('store',11,12,0,4);e('load',6,12,0,4)
    e('mov',9,10);e('addi',9,32);e('addi',9,32);e('store',6,9,12,4)
    e('lrw',4,(DST-32) | 0xa0000000);e('mov',8,10)
    e('addi',8,32);e('addi',8,32);e('addi',8,32)
    e('lrw',5,GUARD_SIZE);e('bsr','copy')
    # The next native-kernel request after the first GA completion is 4BH.
    # Capture it in the same power cycle, without a write-enable operation.
    e('lrw',13,0xa0302000);e('load',11,13,0,4)
    e('lrw',14,0x8420c01f);e('store',14,13,0,4)
    e('lrw',14,0x8423001f);e('lrw',2,0x4b);e('bsr','spi')
    e('lrw',14,0x8423c01f);e('movi',2,0);e('bsr','spi')  # zero address + dummy
    e('lrw',4,'reply-uid')
    for off in (0,4,8,12):
        e('lrw',2,0xffffffff);e('bsr','spi');e('store',2,4,off,4)
    e('lrw',4,'reply-banks')
    for bank in (0x1000,0x2000,0x3000):
        e('lrw',6,0x8420c01f);e('store',6,13,0,4)
        e('lrw',14,0x8423c01f);e('lrw',2,0x48000000|bank);e('bsr','spi')
        e('lrw',14,0x8423001f);e('movi',2,0);e('bsr','spi')
        e('lrw',14,0x8423c01f);e('lrw',12,256)
        a.label('bank-%x'%bank)
        e('lrw',2,0xffffffff);e('bsr','spi');e('store',2,4,0,4)
        e('addi',4,4);e('subi',12,1);e('cmpnei',12,0);e('bt','bank-%x'%bank)
    e('br','spi-complete')
    a.label('spi-timeout')
    e('load',6,10,20,4);e('bseti',6,1);e('store',6,10,20,4)
    a.label('spi-complete')
    e('lrw',6,0x8420c01f);e('store',6,13,0,4)  # always deselect
    e('store',11,13,0,4)
    e('lrw',9,'crc-table');e('mov',4,10);e('lrw',5,HEADER_SIZE+DATA_SIZE)
    e('lrw',8,0xffffffff);e('bsr','crc')
    e('lrw',6,'reply-crc');e('store',8,6,0,4)
    e('mov',4,10);e('addi',4,16);e('lrw',5,HEADER_SIZE+DATA_SIZE+12-16);e('bsr','send')
    a.label('idle');e('br','idle')
    a.label('spi')
    e('store',14,13,0,4);e('store',2,13,8,4)
    e('mov',6,14);e('bseti',6,12);e('store',6,13,0,4)
    e('lrw',7,1000000)
    a.label('spi-poll')
    e('load',6,13,4,4);e('btsti',6,0);e('bt','spi-ready')
    e('subi',7,1);e('cmpnei',7,0);e('bt','spi-poll');e('br','spi-timeout')
    a.label('spi-ready')
    e('store',14,13,0,4);e('load',2,13,12,4);e('jmp',15)
    a.label('copy')
    e('load',6,4,0,4);e('store',6,8,0,4)
    e('addi',4,4);e('addi',8,4);e('subi',5,4);e('cmpnei',5,0);e('bt','copy');e('jmp',15)
    a.label('crc')
    e('load',6,4,0,1);e('xor',8,6);e('mov',7,8);e('zextb',7);e('lsli',7,2)
    e('addu',7,9);e('load',7,7,0,4);e('lsri',8,8);e('xor',8,7)
    e('addi',4,1);e('subi',5,1);e('cmpnei',5,0);e('bt','crc');e('not',8);e('jmp',15)
    a.label('send')
    e('load',6,3,20,4);e('btsti',6,6);e('bf','send')
    e('load',2,4,0,1);e('store',2,3,0,4);e('addi',4,1);e('subi',5,1)
    e('cmpnei',5,0);e('bt','send');e('jmp',15)
    header = MAGIC + struct.pack('<4I',2,0,DATA_SIZE,0) + bytes(48) + struct.pack('<4I',SRC,DST,CMD,80)
    assert len(header)==HEADER_SIZE
    code=a.finish({'source':struct.pack('<24I',*SOURCE),'initial':struct.pack('<40I',*INITIAL),
                   'job':struct.pack('<20I',*COMMAND),'reply':header+bytes(GUARD_SIZE),'reply-uid':bytes(16),
                   'reply-route':bytes(32),'reply-banks':bytes(3072),
                   'reply-crc':bytes(4),'footer':FOOTER,'crc-table':crc_table()})
    return code,a.labels['idle']


def parse_reply(data):
    if len(data)<17:return None
    legacy=data[1:17]==OLD_MAGIC
    data_size=176 if legacy else DATA_SIZE
    total=1+HEADER_SIZE+data_size+12
    if len(data)<total:return None
    if len(data)!=total or data[:17]!=b'O'+(OLD_MAGIC if legacy else MAGIC) or data[-8:]!=FOOTER:
        raise ValueError('invalid GA probe framing')
    body=data[1:1+HEADER_SIZE+data_size]
    if zlib.crc32(body)!=struct.unpack_from('<I',data,1+len(body))[0]:
        raise ValueError('GA probe CRC mismatch')
    version,status,size,reserved=struct.unpack_from('<4I',body,16)
    if version!=(1 if legacy else 2) or status not in (0,1,2,3) or size!=data_size or reserved or struct.unpack_from('<4I',body,80)!=(SRC,DST,CMD,80):
        raise ValueError('unexpected GA probe metadata')
    before=struct.unpack_from('<4I',body,32);after=struct.unpack_from('<4I',body,48)
    ack=struct.unpack_from('<4I',body,64)
    actual=list(struct.unpack_from('<40I',body,HEADER_SIZE));expected=list(INITIAL)
    for y in range(3):expected[8+y*8:12+y*8]=SOURCE[y*8:y*8+4]
    pixel_indices=[8+y*8+x for y in range(3) for x in range(4)]
    protected_indices=[i for i in range(40) if i not in pixel_indices]
    uid_words=struct.unpack_from('<4I',body,HEADER_SIZE+GUARD_SIZE)
    uid=b''.join(word.to_bytes(4,'big') for word in uid_words)
    result={'timed_out':bool(status&1),'copy_matches':actual==expected,
            'guards_and_padding_unchanged':all(actual[i]==INITIAL[i] for i in protected_indices),
            'irq62_pending':bool(after[3] & (1<<30)), 'irq62_cleared':not bool(ack[1] & (1<<30)),
            'gate_restored':before[3]==ack[3],
            'before':[hex(x) for x in before],'after':[hex(x) for x in after],
            'after_ack':[hex(x) for x in ack], 'output_words':[hex(x) for x in actual],
            'output_sha256':hashlib.sha256(body[HEADER_SIZE:HEADER_SIZE+GUARD_SIZE]).hexdigest(),
            'flash_uid_captured':not bool(status&2),'flash_uid_hex':uid.hex() if not status&2 else None,
            'flash_uid_sha256':hashlib.sha256(uid).hexdigest() if not status&2 else None}
    if not legacy:
        route=struct.unpack_from('<8I',body,HEADER_SIZE+176)
        result['routing']={'source_before':hex(route[0]),'source_during':hex(route[1]),
                           'mask_before':hex(route[2]),'mask_during':hex(route[3]),
                           'low_status_after_ack':hex(route[4]),'high_status_after_ack':hex(route[5]),
                           'source_restored':route[0]==route[6],'mask_restored':route[2]==route[7]}
        result['security_banks']={}
        if not status&2:
            for i,bank in enumerate((0x1000,0x2000,0x3000)):
                words=struct.unpack_from('<256I',body,HEADER_SIZE+208+i*1024)
                raw=b''.join(word.to_bytes(4,'big') for word in words)
                result['security_banks'][hex(bank)]={'size':1024,'crc32':'%08x'%zlib.crc32(raw),
                                                    'sha256':hashlib.sha256(raw).hexdigest(),'data_hex':raw.hex()}
    return result


class ProbeBus(ram.DiagnosticBus):
    def __init__(self, stalled=False, spi_stalled=False):
        super().__init__();self.ga=GraphicsAccelerator(self);self.stalled=stalled;self.gate=0x12345678
        self.spi_stalled=spi_stalled
        from starsat_emu.spi import SPIController, SerialNOR
        self.spi=SPIController(SerialNOR(b''));self.spi.flash.unique_id=bytes(range(16))
        self.spi_commands=[]
        self.route=0x12345678;self.mask=0xffffffff
        for bank in (0x1000,0x2000,0x3000):
            self.spi.flash.security[bank]=bytes((i+(bank>>8))&255 for i in range(1024))
    def access(self,address,size,value=None,fetch=False):
        if not fetch and 0x302000<=address<0x302010:
            if address==0x302004 and value is None and self.spi_stalled:return 0
            if value is not None:
                if address not in (0x302000,0x302008):raise ValueError('unexpected SPI write')
                self.device_writes.append((address,value,size))
            before=self.spi.flash.command
            result=self.spi.access(address-0x302000,size,value)
            if before is None and self.spi.flash.command is not None:
                self.spi_commands.append(self.spi.flash.command)
                if self.spi.flash.command not in (0x4b,0x48):raise ValueError('unexpected flash command')
            return result
        if not fetch and self.ga.base<=address<self.ga.base+self.ga.size:
            if value is not None:self.device_writes.append((address,value,size))
            if self.stalled and address==self.ga.base+0x18 and value==0x10001:
                self.ga.registers[0x18]=value;return
            return self.ga.access(address-self.ga.base,size,value)
        if not fetch and address in (0x500044,0x500014,0x500000,0x500004,0x500054,0x50009c):
            if size!=4:raise ValueError('unexpected interrupt access size')
            if value is None:
                if address==0x500044:return self.gate
                if address==0x50009c:return self.route
                if address==0x500054:return self.mask
                pending=(1<<30) if self.ga.irq_pending() and self.gate & (1<<30) and (self.route>>16)&63==62 else 0
                return pending if address==0x500014 else pending & ~self.mask if address==0x500004 else 0
            if address==0x500044 and value in (0x12345678,0x52345678):self.gate=value
            elif address==0x50009c and value in (0x12345678,0x123e5678):self.route=value
            elif address==0x500054 and value in (0xffffffff,0xbfffffff):self.mask=value
            else:raise ValueError('unexpected interrupt write')
            self.device_writes.append((address,value,size));return
        return super().access(address,size,value,fetch)


def check_probe(loader):
    code,idle=program();bus=ProbeBus();cpu=ram.simulate_continuation(loader,code,idle,bus,limit=2000000)
    result=parse_reply(bytes(bus.outgoing))
    if not result or result['timed_out'] or not all(result[k] for k in ('copy_matches','guards_and_padding_unchanged','irq62_pending','irq62_cleared','gate_restored')):
        raise ValueError('offline GA probe transport/sequence check failed')
    if len(bus.ga.history)!=1 or bus.ga.history[0]['words']!=[hex(v) for v in COMMAND]:raise ValueError('GA command changed')
    allowed={0x403000,0x500044,0x500054,0x50009c,0x302000,0x302008,*[0x4700000+off for off in GraphicsAccelerator.offsets]}
    if any(a not in allowed and (a,v,z)!=(0xf80004,1,4) for a,v,z in bus.device_writes):raise ValueError('unexpected MMIO write')
    if bus.spi_commands!=[0x4b,0x48,0x48,0x48] or bus.spi.flash.selected or result['flash_uid_hex']!=bytes(range(16)).hex():raise ValueError('unexpected flash auxiliary read sequence')
    if not all(result['routing'][k] for k in ('source_restored','mask_restored')):raise ValueError('interrupt routing cleanup failed')
    for bank,raw in bus.spi.flash.security.items():
        if result['security_banks'][hex(bank)]['data_hex']!=raw.hex():raise ValueError('security-bank data differs')
    return {'steps':cpu.steps,'code_sha256':hashlib.sha256(code).hexdigest(),'result':result,
            'limitations':'hypothesis DMA/IRQ fixture validates program framing and bounded sequence, not physical graphics semantics'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',required=True);parser.add_argument('--loader',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=7200)
    parser.add_argument('--output',type=Path,default=Path('traces')/datetime.now(timezone.utc).strftime('serial-graphics-probe-%Y%m%dT%H%M%S%fZ'))
    args=parser.parse_args()
    print('Checking fixed RAM-only graphics probe locally...',flush=True);offline=check_probe(args.loader.read_bytes())
    print('Local command, buffer, CRC and interrupt-gate checks passed.',flush=True)
    boot_prefix=Path(str(args.output)+'-stage1');boot=stage1.probe(args.port,args.loader,args.seconds,boot_prefix)
    if boot['reason']!='stage1-ready':return 3
    result=ram.run(args.port,Path(str(boot_prefix)+'.json'),args.loader,args.output,
        packet_factory=lambda:ram.wrap_stage2(program()[0]),verifier=lambda loader:offline if hashlib.sha256(loader).hexdigest()==stage1.LOADER_SHA256 else check_probe(loader),
        reply_parser=parse_reply,mode='fixed-ram-graphics-copy-irq-probe',seconds=30,result_key='graphics',success_reason='graphics-probe-received')
    return 0 if result['reason']=='graphics-probe-received' else 3


if __name__=='__main__':raise SystemExit(main())
