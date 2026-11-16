# Graphics investigation, 2026-11-14

Status: the unscaled RGBA copy mode has passed a physical RAM experiment.
A diagnostic emulator continuation now produces its first real firmware
dialog. IRQ routing remains inferred from firmware and the public controller
driver; its physical experiment timed out. The standard receiver profile
now enables the measured copy model and captured identity data. No menu has been
demonstrated. The sections below retain the investigation's earlier hypotheses
and distinguish them from subsequent measurements.

The two captured cold-machine states contain one 80-byte job addressed by
GA `0x04700004`, with its byte count at `0x04700008`. In both images the
job destination is exactly the current OSD descriptor's pixel address.
The source and destination currently contain zeros. See
`traces/graphics-command-state-20261114.json`.

## Firmware evidence

In the receiver-backup kernel:

- `0x9013FFA8` packs an internal job object into twenty little-endian words.
  Stores at `0x9014029C..0x90140380` establish the buffer and geometry fields.
- `0x9013FEAA..0x9013FF14` disables the engine, prepares a list, enables it,
  writes command count to GA `+0x18`, and raises its bit 16 to start. It
  then waits on the completion semaphore at context offset 520.
- The interrupt wrapper at `0x9013D230` explicitly requires vector **62**
  and calls `0x9014377C`. That handler toggles bit 1 of GA `+0x1C`, updates
  the completed queue count and posts the same semaphore.
- The provisional emulator executes the initial copy and supplies that level
  interrupt only after guest RAM is updated. The actual guest ISR releases
  the semaphore; no guest check, PC or semaphore value is patched.
  This hypothesis reaches a new read of flash opcode **4BH**, which requires
  the device's factory ID. Report:
  `traces/receiver-graphics-hypothesis-20261114.json`.

The initial mode word is `0x90400707`; formats in word 0 bits 5:0 and 13:8,
and word 3 bits 5:0, are all 7. Source-copy behavior for this mode remains a
hypothesis until the hardware experiment establishes its alpha/byte semantics.

| Word | Decoded fields from firmware packing |
| --- | --- |
| 12 | First source address |
| 13 | Second source/background address |
| 14 | Destination address |
| 15 | Source 1 pitch at bits 31:18; width at 17:4 |
| 16 | Source 1 height at 31:18; source 2 pitch at 17:4 |
| 17 | Source 2 width at 31:18; height at 17:4 |
| 18 | Destination pitch at 31:18; width at 17:4; low control nibble |
| 19 | Destination height at 31:18; other flags below |

The observed full-frame jobs use pitch/width 720, height 576 and a low
control nibble of 1. Their auxiliary fields are retained in capture artifacts.
The model stops on unsupported modes, formats, scaling, unknown fields or
out-of-range DMA; it does not report those jobs as complete.

## Fixed physical experiment

`tools/serial_graphics_probe.py` generates a fixed RAM program that:

1. Initializes two scratch DRAM buffers through uncached aliases and submits
   the observed mode with a 4×3 rectangle, eight-pixel pitch and alpha values
   FF/80/00/40. Guard words and row padding distinguish bounds/stride errors.
2. Enables only IRQ62's gate temporarily, polls the corresponding high pending
   bit with a finite budget, records GA state and pending bits, acknowledges
   the completion and restores the original gate. It disables GA afterward.
3. Reads the separate 128-bit factory flash ID with opcode 4BH, zero address,
   one dummy byte and sixteen output bytes. It deselects flash and restores
   its original controller configuration.
4. Sends the captured RAM words, register snapshots and ID with CRC32 framing,
   then idles in RAM. Timeouts remain explicit results.

There is no flash write-enable, program, erase, status write, OTP operation or
arbitrary upload/execute command in this diagnostic. Its only flash opcode
is the read-only 4BH operation. The existing pinned first-stage loader is
unchanged; its previously documented controller setup limitations still apply.

The factory-ID protocol follows GigaDevice GD25Q32C DS-00088 Rev4.1 §7.28
(page 39), retrieved from the manufacturer's PDF. The 4 MiB main-array backup
and the earlier 48H security-bank capture do not contain this separate ID.

The offline program check executes the unchanged stage-1 continuation and
verifies the exact job, command whitelist, frame checksum and cleanup sequence.
Its DMA/IRQ fixture represents the hypothesis and **does not establish physical
pixel semantics**. Result: `traces/graphics-probe-offline-20261114.json`.
The probe is ready to arm after the receiver is physically powered off.


## First physical probe result

`traces/serial-graphics-probe-20261114T005330142437Z.json` is a successful
CRC-framed capture. All twelve RGBA words matched the source exactly, including
alpha values 00/40/80/FF; guard words and row padding were unchanged. This
establishes the unscaled copy mode and pitch interpretation. Cold GA controls
at +10/+18/+1C were all zero. Completion changes +1C from 101 to **111**;
writing 103 clears completion and reads back 103. The completion status is
therefore bit **4**, and acknowledge bit 1 is retained, unlike the initial
model hypothesis.

The factory ID read succeeded and is now in the observation profile. Its hash
is `4a7988dc51d89928ab81359737f7d8b7f1ec2bc54f6fdcd00c3c3f3173c3d9b0`.
No flash mutation commands were sent.

The IRQ poll timed out. The first test omitted the interrupt source selector
and mask configuration normally performed by the kernel. This is a possible
explanation, not a confirmed hardware fault. The revised fixed probe initializes
output 62's selector byte in SOURCE15 and unmasks its bit before enabling its
gate. It restores all three original registers afterward. Routing is now
implemented in the interrupt-controller model instead of assuming identity.

The original firmware's next required read is security bank 1 at 0x1000.
STAR-GA2 combines the revised IRQ measurement with read-only 4BH and 48H
captures for all three 1024-byte banks, to avoid successive power cycles for
additional auxiliary data. Bank 3 can be compared against the previous capture.
The fixture verifies the exact command sequence and all three byte streams;
it remains an offline fixture rather than physical validation.


## Revised combined probe timed out

`traces/serial-graphics-probe-20261114T012520448515Z.json` sent the full
13,712-byte transport but received only the stage-1 acknowledgment and
STAR-GA2 startup marker (17 bytes). No full result frame or checksum arrived.
Its auxiliary banks and interrupt outcome are unknown and have not been used
as measurements. The previous successful copy and factory-ID capture remain
valid. The cause of the stall is not established.

A separate `serial_flash_dump.py --security-all` reader now uses only the
previously working SPI/UART path, reads the three 1024-byte banks in order,
and returns one CRC-checked 3072-byte capture. It does not change GA or
interrupt registers. `security-all-reader-offline-20261114.json` checks the
exact separate bank contents and command boundaries through the pinned
stage-1 continuation. It does not assert that the timed-out test succeeded.

## Security capture and first guest dialog

`traces/serial-flash-dump-20261114T013210843939Z.json` successfully captured
all three security banks with CRC32 **0e72b35b** over the 3072 bytes. Bank 3
is identical to the earlier independent capture. The exact bytes, including
the measured erased first bank, are imported into both observation profiles.
No flash mutation commands were sent.

The diagnostic GA continuation then executes three guest copy jobs and writes
4,976,640 pixel bytes. `traces/receiver-first-graphics-20261114.png` is rendered
from guest RAM and shows **Start to Burn SN,don't power off**. This is the
virtual firmware's provisioning dialog, not a command issued to the receiver.
At 2,555,346,284 instructions, PC 0x900B82C8, the virtual OTP model blocks
the attempted programming write. The report is
`traces/receiver-graphics-progress-20261114.json`. No PC, semaphore or identity
check was patched. This is the first visible dialog, not a working menu.

OTP still contains explicitly synthetic zero bytes at that boundary. A fixed
read-only reader, `tools/serial_otp_read.py`, follows the native read routine
at 0x900B8150: wait for ready bit 10, wait for busy bit 8 to clear, pulse READ
bit 14 with the 11-bit byte address, clear READ, wait for DONE bit 9, collect
the status low byte and clear control. It never sets programming bit 15 or
the programming-data field and never writes GCTL programming controls.
All polls have finite budgets. It disables CPU IRQ and frames its header and
data with CRC32. Offline validation through the pinned stage-1 continuation
is `traces/otp-reader-offline-20261114.json`; this verifies the code against a
fixture, not the receiver's physical values.

The physical capture subsequently succeeded:
`traces/serial-otp-read-20261114T014908884759Z.json`, 2048 bytes, CRC32
**0166a8a5**, SHA256
`00b172e697cdf7afab5cdfaf95e6a7ae7c8c7a95dc08a72883894da5997ed3fc`,
auxiliary info **D2**. Both profiles now contain these exact bytes, and a
fresh receiver cold boot gets past the serial-provisioning decision. OTP
programming remains blocked. The next boundary is documented in
`docs/av-microcode-notes.md`; the resident register monitor is running again.
