#!/usr/bin/env python3
"""Fixed read-only GX6605S OTP RAM reader; programming bit 15 is never set.

The address field/read sequence follows the native firmware. Only READ bit 14
and an 11-bit byte address are written to the controller. No OTP configuration,
programming data, flash, GCTL, or interrupt-controller writes are issued by the
custom code. CPU IRQ is disabled while it runs. Every poll has a finite budget.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from csky_ram_asm import Assembler
from serial_flash_dump import crc_table
import serial_ram_diagnostic as ram
import serial_ram_probe as stage1
from starsat_emu.otp import OTP

MAGIC=b'STAR-OTPR1'.ljust(16,b'\0')
FOOTER=b'END-OTP1'
SIZE, HEADER_SIZE=2048,48


def program(size=SIZE,poll_budget=100000):
    if type(size) is not int or not 1<=size<=SIZE or size%4:raise ValueError('OTP capture must be word aligned within the 11-bit address window')
    if type(poll_budget) is not int or not 1<=poll_budget<=100000:raise ValueError('invalid OTP poll budget')
    a=Assembler(ram.ENTRY);e=a.emit
    e('lrw',3,0xa0403000);e('lrw',10,'reply')
    e('mov',4,10);e('movi',5,16);e('bsr','send')
    e('mfcr',6,0);e('store',6,10,40,4);e('psrclr',1)
    e('lrw',13,0xa0f80000)
    e('load',6,13,12,4);e('store',6,10,32,4)
    e('load',6,13,8,4);e('store',6,10,36,4)
    e('movi',12,0);e('lrw',11,size);e('lrw',4,'payload')
    def poll(label,bit,present,error):
        e('lrw',8,poll_budget);a.label(label)
        e('load',6,13,8,4);e('btsti',6,bit)
        e('bt' if present else 'bf',label+'-ok')
        e('subi',8,1);e('cmpnei',8,0);e('bt',label)
        e('movi',6,error);e('store',6,10,28,4);e('br','finish')
        a.label(label+'-ok')
    poll('ready',10,True,1)
    a.label('next-byte')
    poll('busy',8,False,2)
    e('mov',6,12);e('lsli',6,3);e('bseti',6,14);e('store',6,13,0,4)
    e('movi',5,100);a.label('read-delay');e('subi',5,1);e('cmpnei',5,0);e('bt','read-delay')
    e('mov',6,12);e('lsli',6,3);e('store',6,13,0,4)
    poll('done',9,True,3)
    e('load',6,13,8,4);e('store',6,4,0,1);e('addi',4,1)
    e('movi',6,0);e('store',6,13,0,4)
    e('addi',12,1);e('cmpne',12,11);e('bt','next-byte')
    a.label('finish')
    e('movi',6,0);e('store',6,13,0,4);e('store',12,10,44,4)
    e('lrw',9,'crc-table');e('mov',4,10);e('lrw',5,HEADER_SIZE+size)
    e('lrw',8,0xffffffff);e('bsr','crc');e('lrw',6,'reply-crc');e('store',8,6,0,4)
    e('mov',4,10);e('addi',4,16);e('lrw',5,HEADER_SIZE+size+12-16);e('bsr','send')
    a.label('idle');e('br','idle')
    a.label('crc')
    e('load',6,4,0,1);e('xor',8,6);e('mov',7,8);e('zextb',7);e('lsli',7,2)
    e('addu',7,9);e('load',7,7,0,4);e('lsri',8,8);e('xor',8,7)
    e('addi',4,1);e('subi',5,1);e('cmpnei',5,0);e('bt','crc');e('not',8);e('jmp',15)
    a.label('send')
    e('load',6,3,20,4);e('btsti',6,6);e('bf','send')
    e('load',2,4,0,1);e('store',2,3,0,4);e('addi',4,1);e('subi',5,1);e('cmpnei',5,0);e('bt','send');e('jmp',15)
    header=MAGIC+struct.pack('<8I',1,0,size,0,0,0,0,0)
    code=a.finish({'reply':header,'payload':bytes(size+(4-size%4)%4),
                   'reply-crc':bytes(4),'footer':FOOTER,'crc-table':crc_table()})
    return code,a.labels['idle']


def parse_reply(data,size=SIZE):
    total=1+HEADER_SIZE+size+12
    if len(data)<total:return None
    if len(data)!=total or data[:17]!=b'O'+MAGIC or data[-8:]!=FOOTER:raise ValueError('invalid OTP capture framing')
    body=data[1:1+HEADER_SIZE+size]
    if zlib.crc32(body)!=struct.unpack_from('<I',data,1+len(body))[0]:raise ValueError('OTP capture CRC mismatch')
    version,start,amount,status,info,initial,psr,count=struct.unpack_from('<8I',body,16)
    if version!=1 or start!=0 or amount!=size or count>size:raise ValueError('invalid OTP capture metadata')
    if status or count!=size:raise ValueError('OTP read failed: status=%d, captured=%d/%d bytes'%(status,count,size))
    payload=body[HEADER_SIZE:]
    return {'size':size,'start':0,'otp_info':hex(info),'initial_status':hex(initial),'initial_psr':hex(psr),
            'crc32':'%08x'%zlib.crc32(payload),'sha256':hashlib.sha256(payload).hexdigest()}


class ReaderBus(ram.DiagnosticBus):
    def __init__(self,data,stalled=False):
        super().__init__();self.otp=OTP(0x12345678);self.otp.data=bytes(data).ljust(SIZE,b'\0');self.stalled=stalled;self.addresses=[]
    def access(self,address,size,value=None,fetch=False):
        if not fetch and address in (0xf80000,0xf80008,0xf8000c):
            if value is not None:
                if address!=0xf80000 or size!=4 or value & ~0x7ff8 or value & 0x8000:raise ValueError('OTP diagnostic attempted a non-read operation')
                self.device_writes.append((address,value,size))
                if value & 0x4000:self.addresses.append((value>>3)&0x7ff)
            if self.stalled and address==0xf80008 and value is None:return 0
            return self.otp.access(address-0xf80000,size,value)
        return super().access(address,size,value,fetch)


def check_reader(loader):
    image=bytes((i*29+7)&255 for i in range(SIZE));code,idle=program();bus=ReaderBus(image)
    cpu=ram.simulate_continuation(loader,code,idle,bus,limit=3000000)
    result=parse_reply(bytes(bus.outgoing))
    if bus.addresses!=list(range(SIZE)) or bus.outgoing[1+HEADER_SIZE:1+HEADER_SIZE+SIZE]!=image:raise ValueError('OTP read sequence/data differs')
    if any(a not in (0x403000,0xf80000) and (a,v,z)!=(0xf80004,1,4) for a,v,z in bus.device_writes):raise ValueError('unexpected MMIO write')
    return {'steps':cpu.steps,'code_sha256':hashlib.sha256(code).hexdigest(),'read_bytes':SIZE,'programming_bit_never_set':True,
            'result':result,'limitations':'native read protocol inferred from firmware; fixture validates code/CRC and absence of programming operations, not physical fuse values'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',required=True);parser.add_argument('--loader',type=Path,required=True);parser.add_argument('--seconds',type=float,default=7200)
    parser.add_argument('--output',type=Path,default=Path('traces')/datetime.now(timezone.utc).strftime('serial-otp-read-%Y%m%dT%H%M%S%fZ'))
    args=parser.parse_args();print('Checking fixed read-only OTP RAM program...',flush=True);offline=check_reader(args.loader.read_bytes())
    print('Local data/CRC checks passed; only read control values are permitted.',flush=True)
    prefix=Path(str(args.output)+'-stage1');boot=stage1.probe(args.port,args.loader,args.seconds,prefix)
    if boot['reason']!='stage1-ready':return 3
    report=ram.run(args.port,Path(str(prefix)+'.json'),args.loader,args.output,
                   packet_factory=lambda:ram.wrap_stage2(program()[0]),verifier=lambda loader:offline if hashlib.sha256(loader).hexdigest()==stage1.LOADER_SHA256 else check_reader(loader),
                   reply_parser=parse_reply,mode='fixed-read-only-otp-capture',seconds=30,result_key='otp',success_reason='otp-capture-received')
    if report['reason']!='otp-capture-received':return 3
    raw=Path(str(args.output)+'.bin').read_bytes();path=Path(str(args.output)+'.otp.bin')
    with path.open('xb') as out:out.write(raw[1+HEADER_SIZE:1+HEADER_SIZE+SIZE])
    print('Verified read-only OTP copy saved: '+str(path),flush=True);return 0


if __name__=='__main__':raise SystemExit(main())
