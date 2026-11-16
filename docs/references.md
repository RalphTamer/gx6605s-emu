# References and provenance

## CPU instruction reference

The interpreter's supported ABIv1 encodings and operations were implemented
with reference to Alibaba/T-Head's QEMU C-SKY translator and helpers:

- Repository: https://github.com/T-head-Semi/qemu
- Branch examined: `xuantie-qemu-9.0`
- Commit: `3287d345c7f5d60d5c8774d90752f5f710744f85`
- [translate_v1.c](https://github.com/T-head-Semi/qemu/blob/3287d345c7f5d60d5c8774d90752f5f710744f85/target/csky/translate_v1.c)
- [op_helper_v1.c](https://github.com/T-head-Semi/qemu/blob/3287d345c7f5d60d5c8774d90752f5f710744f85/target/csky/op_helper_v1.c)
- [mmu.c](https://github.com/T-head-Semi/qemu/blob/3287d345c7f5d60d5c8774d90752f5f710744f85/target/csky/mmu.c):
  software TLB operations, page sizes, permissions and direct supervisor segments.
- `cpu.c`: MSA0/MSA1 reset values used by the experimental CPU.

The translator identifies Copyright (c) 2024 Alibaba Group and is licensed
LGPL-2.0-or-later. `starsat_emu/cpu.py` retains that license designation for the
Python adaptation of the supported decoding/semantics. `starsat_emu/mmu.py`
also retains LGPL-2.0-or-later for its adaptation. See `COPYING.LESSER`.
The RAM-only C adaptation in `native/csky_fast.c` retains the same designation
and copyright notice. Its source is copied into the application resources.
This adaptation has not been checked against a running QEMU CPU engine.

The `c-sky/qemu` repository's default branch was also inspected; it did not
contain `target/csky`. Use the T-Head fork when investigating a future backend.

## GX6605S register and startup references

The xboot GX6605S port is MIT licensed, Copyright (c) 2021 Sanpe
<sanpeqf@gmail.com>. Register addresses and startup descriptions were consulted
from the files below; no xboot source files are bundled in this project.

- Repository: https://github.com/GX-OSS/xboot-gx6605s
- Commit examined: `30e7b914537f4e99da36b9941afca6aed2d5f7ae`
- `src/arch/csky/mach-gx6605s/include/gx6605s.h`: memory segments, physical bases,
  boot magic, and `IMAGE_POS` (four-byte magic prefix).
- `include/gx6605s-uart.h`: control/status/data/clock/config registers;
  transmit-empty is bit 7 and receive-ready is bit 5.
- `include/gx6605s-intc.h`: IRQ/FIQ mask offsets `0x50`, `0x54`, `0x150`, `0x154`.
- `include/gx6605s-gctl.h`: source selector, PLL, divider, and DTO offsets.
- `sys-ccu.c`: PLL configuration write/readback sequence and DTO programming.
- `sys-dram.c`: Denali START bit and initialization-complete bit.
- `include/gx6605s-spi.h` and `sys-spinor.c`: controller transfer length, chip
  select, START/READY, and TX/RX FIFO byte order.
- `include/gx6605s-rtc.h`: RTC counter and configuration register layout.
- `include/gx6605s-cir.h`: infrared receiver register layout and empty FIFO status.
- `include/gx6605s-vpu.h` and `driver/video/gx6605s-osd.c`: OSD register,
  descriptor, stride, byte-order, color-format and linked-list layouts used by
  the software scanout. Hardware scaling and video decoding are absent.
- `include/gx6605s-gpio.h` and `driver/gpio-gx6605s.c`: GPIO direction,
  data, output set/clear and inversion registers.
- `start.S`: ABIv1 startup sequence and MMU setup examples.

These references describe a GX6605S port; they do not prove the StarSat board's
BootROM contents, oscillator configuration, register reset values, or exact
peripheral wiring.

[C-SKY GX6605S development-board documentation](https://c-sky.github.io/docs/gx6605s.html)
identifies a C-SKY ISA V1 CK610M CPU with 64 MiB of integrated DDR2. The
experimental machine uses that RAM capacity, without asserting that the
receiver's accessible memory configuration has been verified.

## Evidence from the local firmware

Input SHA-256:
`10c0962a5f1fef6a5d0313e5e795dd376b9148cc4221696db9c2e99471b4d5b3`.

The native UART address is established by guest accesses, independently of the
generic `ns16550a` UART at `0x00403000` mentioned in development-board Linux
device trees. These are distinct interfaces; the prototype models the native
GX UART at `0x00400000`.

The observed `RUN` comes from the firmware executing guest instructions that
write to the UART data register. It is not a printed substitute for booting.
The provisional initial memory/register state still limits what that execution
establishes about behavior on real hardware.

The controller at physical `0x00F80000` is treated as a read-only synthetic OTP
interface. The inferred read protocol waits for bit 10 at offset 8, requests
an address through offset 0 using a bit-14 pulse, then waits for bit 9 and
extracts the returned low byte. This is inferred from the routine at virtual
`0x93FB6834` in the loaded bootloader. It is not a manufacturer specification.
The synthetic fuse data is zero-filled, not a receiver dump, and no secrets or
authentication-success responses are supplied. Offset `0x0C` is an explicit
profile-supplied synthetic information word. The bootloader extracts bits 11..13
as a selector; the exploratory profile selects zero without claiming it was measured.

GCTL offset `0xA04` is an undocumented configuration access observed in the
same firmware. It is separately named as an observed register in
`starsat_emu/gctl_registers.json`; its zero reset value is an exploratory
assumption. VPU storage and zero reads also remain assumptions, recorded in
reports. These hypotheses are not evidence that modified firmware is flashable.

The `3B` dual-output opcode and receive-mode bit 22 are observed directly in
the firmware's SPI transactions. Eight dummy clocks require two FIFO bytes
in dual receive mode. Correcting this preserved the partition-table marker and
made the complete loaded BOOT/KERNEL buffers match the input image.

Payload decoding reads a 128-byte block at physical `0x04800140` through the
custom digest routine at virtual `0x93FBA2C8`. The consulted VPU header does
not establish this block's reset/readback semantics. Missing values now stop
explicitly. See [boot-status.md](boot-status.md) for the observed failing check
and the limits of the zero-value diagnostic experiment.

## Additional software-only investigation

The C-SKY vendor-driver example supplies an independent VPU register map:

- Repository: `c-sky/addons-linux`, commit
  `5c6e7476aac7701c15458c01066b04fecbcf0d2f`.
- [gx6605s.h](https://github.com/c-sky/addons-linux/blob/5c6e7476aac7701c15458c01066b04fecbcf0d2f/addons/drivers/video/fbdev/nationalchip/gx6605s.h)
  labels `0x104..0x1FC` reserved. This does not specify reset values or prove
  that reads return zero.
- [gx6605s_vpu_reg.h](https://github.com/c-sky/addons-linux/blob/5c6e7476aac7701c15458c01066b04fecbcf0d2f/addons/drivers/video/fbdev/nationalchip/gx6605s_vpu_reg.h)
  defines `VPU_SET_RW_BYTESEQ`: the low two sequence bits are in SYS_PARA
  bits 21:20 and the high bit is bit 28. The OSD renderer now decodes this
  split field correctly, including CDAB_GHEF descriptors used for 16-bpp output.

The M-CORE interpreter in `linux-goxceed/binutils-gdb/sim/mcore/interp.c`
was also inspected as a comparison for shared integer instructions. M-CORE
is not a complete independent C-SKY CPU reference, and this inspection is
not a differential QEMU execution test. No source from that simulator is bundled.

## Physical GX6605S UART bootstrap investigation

- [rva3/gx6605s-loader](https://github.com/rva3/gx6605s-loader) provides a
  GX6605S-specific reverse-engineered ROM upload implementation. Its first
  stage uses an 8 KB SRAM layout and waits for `RUNGET` before stage 2.
- [matu6968/libre-gxdl](https://github.com/matu6968/libre-gxdl/tree/0cf702936ecb35234d116beff43c8039e67e48bb)
  supplies the externally reviewed `gx6605s-6605s-sflash-27M.boot` artifact.
  Its header identifies chip family `0x3211` and baud 115200. This is a vendor
  binary hosted by a third party, not a manufacturer compatibility guarantee;
  no artifact is bundled here. The author's reported hardware testing covers
  other GX families. See boot-status for its pinned hash and review limits.
- Second-stage packaging differs between the two implementations. The new
  physical probe sends only stage 1 and no console or flash command. Neither
  source establishes the installed StarSat firmware's exact command behavior.

## Timer and kernel-device bring-up

- [Linux timer-gx6605s.c](https://github.com/torvalds/linux/blob/master/drivers/clocksource/timer-gx6605s.c)
  documents the two 32-bit count-up timers at relative offsets 0 and `0x40`,
  the status/value/control/config/divider/initial-count registers, overflow
  events, reset/start bits, and write-one status clearing. Copyright 2018
  Hangzhou C-SKY Microsystems, GPL-2.0. The emulator implements a separate
  behavioral model; no Linux source is bundled. Its input-clock rate and
  instruction-based virtual-time scaling are assumptions; IRQ delivery is absent.
- The pinned xboot GX6605S `include/gx6605s-i2c.h` identifies relative offset
  `0x08` as the I2C clock register. The installed kernel's next unmodeled
  initial clock setup writes physical `0x00205008`, value `0x00420042`. This
  controller is now modeled with an empty virtual bus; no slave ACK or tuner
  response is synthesized.
- The same xboot commit's `driver/i2c-gx6605s.c` supplies polling control
  sequences: DONE is bit 3 and WACK set indicates NACK; STOP completion is
  polled separately. The emulator's empty-bus model and three instances are
  explicit limitations. Instance addresses come from the extracted eCos
  device table and actual kernel initialization, not a guessed dense region.
- Native UART offset `0x14` is an observed kernel configuration extension
  beyond xboot's five-register UART header. Only initialized storage/readback
  is modeled there; its reset and electrical semantics remain unknown.


## Further kernel bring-up, 2026-11-14

- The same pinned xboot commit's `driver/video/gx6605s-vpu.c` has the VOUT
  TV_FORMAT/FRAME_FRE table: 576p is format 9; 1080i50 is format 6 plus bit 5.
  [Source](https://github.com/GX-OSS/xboot-gx6605s/blob/30e7b914537f4e99da36b9941afca6aed2d5f7ae/src/arch/csky/mach-gx6605s/driver/video/gx6605s-vpu.c).
  Scan-field bit 11 is independently observed in physical interlaced samples;
  progressive frame parity remains an inference from native kernel calibration.
- The pinned T-Head QEMU `translate_v1.c`, `op_helper_v1.c`, `helper.c` and
  `cpu.h` provide the ABIv1 PSR bank, IDLY4, IRQ entry and RTE references.
  Timer IRQ routing uses line 10 and autovector 10. Other exception paths
  and FIQ delivery remain incomplete.
- Linux's [EHCI register definitions](https://github.com/torvalds/linux/blob/master/include/linux/usb/ehci_def.h)
  and [OHCI register definitions](https://github.com/torvalds/linux/blob/master/drivers/usb/host/ohci.h)
  describe controller fields and W1C/enable aliases. Initial capabilities and
  port states were captured on this receiver. Models were written separately;
  no USB transfer engine or attached device is claimed.
- [Linux GX6605S timer driver](https://github.com/torvalds/linux/blob/master/drivers/clocksource/timer-gx6605s.c)
  names timer fields and clears the initial count in its one-shot IRQ path.
  The periodic reload interpretation is also supported by xboot TIM_RELOAD
  and by the native receiver ISR acknowledging without rearming.
- [GigaDevice GD25Q32C, DS-00088 Rev4.1](https://download.gigadevice.com/Datasheet/DS-00088-GD25Q32C-Rev4.1.pdf),
  section 7.33, describes 48H as a security-register read with three address
  bytes and one dummy byte. Each bank is 1024 bytes with within-bank wrapping.
  Bank 3 starts at 0x3000. These bytes are separate from the 4 MiB main array.
  The new RAM capture reads only bank 3; it issues no program, erase, write-enable,
  lock or status-register-write command.

Physical capture files and the assumptions distinguishing measured baselines
from inferred behavior are listed in the observed profile and boot-status log.
