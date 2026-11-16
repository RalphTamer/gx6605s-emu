# Verified boot status, 2026-11-14

Both firmware images now run the kernel scheduler and enable guest OSD.
The receiver-backup continuation renders a **720×576** guest frame; V3.93
renders **1920×1080** with approximate upscaling. **Both frames are currently
blank. Receiver menus, remote interaction and full hardware emulation are
not working yet.** Enabled scanout is a narrower milestone than a working UI.

The latest profile includes CRC-checked chip-register observations and the
receiver's separate 1 KiB flash security bank. Timer IRQ delivery, RTC BCD,
USB controller reset/status, video mode changes and a volatile virtual NOR
write overlay now let initialization proceed further. Graphics command DMA,
USB device transfers, tuner devices and remote-control input remain incomplete.
Opaque MMIO writes remain a diagnostic tracing approximation.

Latest continuation reports: `traces/receiver-monitor3-progress.json` and
`traces/v393-monitor3-progress.json`. Actual guest frame artifacts:
`traces/receiver-guest-osd-20261114.png` and
`traces/v393-guest-osd-20261114.png`. Fresh-boot checks and their I2C timeout
limit are described in the newest section at the end of this document.
The original update and captured main flash have not been changed.

## Earlier software-only boundary, 2026-11-03

Input: `SR-4040HD_VEGA_V3.93_14102026.bin`, SHA-256
`10c0962a5f1fef6a5d0313e5e795dd376b9148cc4221696db9c2e99471b4d5b3`.

The exploratory profile now reaches **61,634,489 executed guest instructions**
and stops at PC `0x93FAD85E` on a read of physical `0x04800150`. The exact
machine/profile state is in `traces/exploratory.json`. UART output is `RUN`.
There is no receiver menu or guest OSD frame at this boundary.

## Fixes that moved startup forward

- Added explicitly synthetic OTP information-word access, GPIO state, and a
  sparse list of provisional configuration words observed during USB PHY/VOUT
  initialization. They do not claim to reproduce electrical hardware.
- Implemented SPI `3B` dual-output reads. Eight dummy clocks occupy two bytes
  in the dual-lane RX FIFO. Consuming only one shifted partition data by a byte.
- The accepted table contains BOOT, TABLE, KERNEL, ROOT, and DATA entries.
  KERNEL starts at payload offset `0x18000` (file `0x18100`), has capacity
  `0x27E000`, and stored data length `0x24AC1C`.
- Verified the loaded KERNEL and BOOT buffers byte-for-byte against the image
  during debugging. BOOT's CRC32 over `0x16E7C` bytes is `0x7FE7ED45`;
  KERNEL's CRC32 over `0x24AC1C` bytes is `0x15F98F3B`, matching table metadata.
- Added native RAM execution to make long decoding loops practical. The fast
  path leaves hardware and unsupported instructions to the Python interpreter.

## Why application startup is still blocked

The ROMFS loader at virtual `0x93FB1688` reads the KERNEL partition and calls
the decoding routine at `0x93FBA4E4`. That routine reads BOOT data and derives
decoding material, including a 20-byte result from the custom digest routine
at `0x93FBA2C8`, with input address `0xA4800140` and length `0x80`.

The input maps to physical VPU registers `0x04800140..0x048001BC`. Guest startup
has written four words at `0x140..0x14C`. Values/readback behavior for the
remaining words are not established by the image or the consulted xboot header.
The default profile now stops at the first missing word, `0x04800150`, instead
of treating this decoding input as ordinary zero-initialized storage.

A diagnostic run with the missing words set to zero executed about 1.51 billion
instructions and reached the bootloader console. A repeated `boot` command
returned `0xFFFFFFFF` from the loader. After three decoding passes, its leading
size word was `0xEF656BB3`, exceeding the loader's `0x002FFFFF` bound, so it
returned to `boot>`. This identifies the failing check; it does not prove that
hardware values are the only remaining issue. CPU interpretation and other
hardware assumptions remain possible contributors. No success check was patched.

The useful missing evidence is the VPU block's actual readback semantics and
values at this stage of startup. A board register dump or an authoritative
register description can establish them. A photo cannot supply those values.
Custom machine profiles accept `video_reset` values, but values written by
the guest may also need real hardware write-mask/readback behavior modeled.

## Reproduce

```sh
sh tools/build_macos.sh
python3 -m unittest discover -s tests -v
python3 -m starsat_emu run SR-4040HD_VEGA_V3.93_14102026.bin \
  --profile exploratory --max-steps 100000000 --report traces/exploratory.json
```

The final command exits with code 2 for the explicit missing-register boundary.
The original image is read-only throughout. The current project cannot establish
that a modified image will boot or be accepted by the receiver.

## Software-only investigation without a serial adapter

An adapter is not a prerequisite for further analysis. It is a way to resolve
missing evidence; software bugs and recoverable hardware behavior remain open
avenues. The current experiments have not established a working application boot.

`tools/probe_decoder.py` now cold-boots the original image to the boundary before
the hardware-dependent digest. It forks the machine state in memory and tests
explicit register-response hypotheses through the actual guest decoder. It does
not patch the guest's success checks, change the firmware, or change the default
machine profile. The addresses are guarded by the original image's SHA-256.

```sh
python3 tools/probe_decoder.py SR-4040HD_VEGA_V3.93_14102026.bin \
  --report traces/offline-decoder-probe.json
```

Nine hypotheses were tested: all zeros, all ones, guest-written values with
zero/one defaults, four masks on writable values, and a repeated first word.
**All nine fail the decoded-size bound; none produces validated ROMFS.** The
complete inputs, decoded headers and outcomes are saved in the report. A small
decoded size would not suffice: the probe also checks ROMFS magic, bounds,
volume-name termination and the superblock checksum before reporting valid ROMFS.
Exit code 3 means none of the candidates validated.

Custom candidate files contain a list of objects with `name` and `registers`.
Each `registers` mapping must explicitly specify all 32 aligned physical words
from `0x04800140` through `0x048001BC`; values may be 32-bit integers or hex
strings. Pass this file with `--candidates path/to/candidates.json`.

A second public register map was consulted and integer instruction behavior
was cross-checked against another simulator's source. This did not identify a
CPU correction that resolves decoding. The public map did expose a separate
OSD bug: the high byte-order bit comes from SYS_PARA bit 28, not bit 22. That
renderer bug is fixed and covered by tests; it does not resolve application startup.

## Physical serial preparation, 2026-11-13

**Readable physical boot output has been captured at 115200 baud, 8N1.**
This establishes a receive-only hardware connection, not a completed emulator.
The receiver reports successful kernel decompression and the user confirms its
normal channel-1 display. The installed firmware differs from the local V3.93
update image; its version is not yet established.

The Mac detects the user's UGREEN 20201 as
`/dev/cu.PL2303G-USBtoUART3110`, using the enabled Prolific driver extension.
Reported adapter measurements relative to breakout terminal 5 are:

- Terminal 2: -9.19 V, consistent with the adapter's idle TX output.
- Terminal 3: approximately 0 V, the receive input to use for listening.

Do not reuse the male-adapter assumption of RX on terminal 2 for this cable.
The receiver's C/yellow wire has ground continuity comparable to shorted meter
leads. A/orange and B/green both read near zero DC volts while powered. Both
have end-to-end wire continuity. With the receiver unplugged, the user measured
14.4 kohm from yellow to orange and 6 kohm from yellow to green. These electrical
measurements alone did not identify TX/RX or establish signal levels/tolerance.
The successful capture below identifies green as receiver TX. Orange is still
only a receiver-RX candidate; it remains disconnected.

Further unpowered resistance measurements, with one probe on orange's free
end and the other on the component pads (upper/lower in the component-side
photo), were reported as:

| Component | Upper pad | Lower pad |
| --- | --- | --- |
| RX1 | 20 kohm | 16 kohm |
| RX2 | 9.73 kohm | 7.3 ohm |
| RX3 | 9.83 kohm | 14.3 kohm |

These suggest orange enters through RX2, approximately 10 kohm, and that
RX2-upper and RX3-upper share a node. A roughly 4.7 kohm RX3 shunt to ground
was initially a hypothesis. Follow-up readings were 16.5 ohm between
RX2-upper and RX3-upper, and 6.5 ohm between RX3-lower and yellow, supporting
those connections. With the adapter's TX2 connected to orange, the user then
measured approximately -3 V at RX2-upper relative to yellow while powered,
consistent with attenuation of the adapter's -9.19 V idle output. These
measurements support the inferred input stage; they are not a complete
schematic or an interface-compliance measurement. The nearby
`1AM` transistor marking is consistent with the onsemi MMBT3904L family
([manufacturer datasheet](https://www.onsemi.com/pdf/datasheet/mmbt3904lt1-d.pdf));
the marking does not uniquely establish manufacturer or the complete circuit.

The serial experiment is receive-only: receiver off while wiring, yellow to
terminal 5 and one signal candidate to terminal 3. The other signal and terminal 2 remain
disconnected, as do all other adapter signal terminals. A silent result does
not prove the receiver is faulty. After the input-divider checks above, the
full test wiring is yellow to GND5, green to adapter RX3, orange to adapter
TX2. All other adapter terminals remain disconnected, and loose soldered
strands need to be secured/insulated. No MMIO-dump console command has been
verified on the installed firmware.

The first orange-to-RX3 trial at 115200 baud is saved as
`traces/serial-20261113T164212186883Z.bin` with its JSON report. Eleven bytes
(`FF FF FF FE FF FF FE FF FF FF FF`) arrived before the user was instructed
to power on. The user then confirmed the normal channel-1 display; no further
bytes arrived before the capture was stopped. This is not decoded UART boot
text and does not establish the orange wire's function.

The successful green-to-RX3 trial is saved as
`traces/serial-20261113T164701878953Z.bin` (1,855 bytes), with `.json` metadata
and an escaped, newline-normalized `.txt` view. Yellow was connected to GND5;
orange, adapter TX2, and all other adapter terminals were disconnected. No
serial bytes were sent. There are a few undecodable initial bytes followed by
readable boot text and later repeated binary records. Their application meaning
is not established.

The physical bootloader reports:

- `GxLoader v1.9 20160325`, CSKY / GX6605S / board 6605s.
- 64 MB RAM, GD25Q32 flash, 4 MB flash size.
- CPU frequency 594 MHz, memory frequency 400 MHz (reported, not measured).
- KERNEL at flash offset `0x10000`, capacity `0x260000`, used size 2,269,211
  bytes, CRC32 `08D0F20A`; TABLE at `0xFE00` and ROOT at `0x270000`.
- `romfs_load file ecos.bin.gz.` followed by `Decompress kernel ...ok`.

The local update image instead has KERNEL at payload offset `0x18000`, capacity
`0x27E000`, and CRC32 `15F98F3B`. The two firmware contents/layouts therefore
cannot be treated as identical. This log confirms the physical platform and
successful physical startup; it does not supply missing VPU readback words or
validate the emulator's CPU/peripheral semantics against the same image.

`tools/serial_capture.py` opens the serial device read-only, configures raw
8N1 without flow control, preserves pending input, and never sends serial
bytes. Opening/closing a serial driver may change modem-control outputs, so
those pins must remain disconnected too. Captured data is saved verbatim in
an exclusively created `.bin` file with a `.json` metadata report. Live text
escapes control bytes; no received terminal escape sequences are executed.

```sh
python3 tools/serial_capture.py --list
python3 tools/serial_capture.py --port /dev/cu.PL2303G-USBtoUART3110 \
  --baud 115200 --seconds 60 --label 'green to adapter RX3; yellow to GND5; TX2 and orange disconnected'
```

Start capture before powering on the receiver. 115200 decoded the physical
boot text successfully. This separate host diagnostic does not connect
physical hardware to the emulator or establish successful firmware emulation.
Four pseudo-terminal/unit tests cover binary input preservation, no transmitted/echoed
data, terminal-setting restoration, escaped display output, and overwrite refusal:

```sh
python3 -m unittest discover -s tests -p test_serial_capture.py -v
```

The separate `tools/serial_boot_probe.py` explicitly enables transmission of
one selected control byte after a new `Partition Version` banner. Its allowed
keys are Escape, Enter, space and Ctrl-C; it cannot send arbitrary console or
flash commands. Stale input is flushed before arming. Transmitted bytes are
recorded in the capture report. Tests verify a fragmented banner triggers
exactly one byte, repeated banners do not retrigger, and runtime data alone
does not trigger transmission. The receive-only CLI retains its read-only
device descriptor and sends no bytes.

```sh
python3 tools/serial_boot_probe.py --port /dev/cu.PL2303G-USBtoUART3110 \
  --key escape --seconds 180
```

Start with receiver power disconnected and meter probes removed, then power
on after the tool reports ARMED. The first 180-second armed window is saved
as `traces/serial-probe-20261113T175034975327Z.bin` with its JSON report. It
received 512 bytes of repeating runtime records but no new boot banner, so
**zero bytes were transmitted**. This did not test whether Escape interrupts
startup.

The subsequent window `traces/serial-probe-20261113T175417619125Z.bin`
captured a real cold restart. It received 2,334 bytes and transmitted exactly
one Escape (`1B`) at `2026-11-13T17:57:13.398407Z`, after 520 received bytes.
The receiver continued to `Decompress kernel ...ok` and runtime binary output;
no `boot>` prompt appeared. The report and escaped `.txt` view are saved beside
the raw log. This failed to interrupt startup but does not distinguish an
ignored/late key from a receive-path issue. The capture is stopped. Successful
host transmission alone does not establish receiver input or console access.

The cold-restart log also contains the binary prefix `00 58` before the
partition banner. A separate `tools/serial_ram_probe.py` is prepared to answer
this earlier ROM handshake using only the first RAM bootstrap stage. It
requires the exact 148,012-byte `gx6605s-6605s-sflash-27M.boot` artifact from
`matu6968/libre-gxdl` commit `0cf702936ecb35234d116beff43c8039e67e48bb`,
SHA256 `0e9e4be311b445ae818585d93c889495fe0d33d794cb1966690bb17963a51631`.
The third-party artifact is kept outside this repository and is not bundled.

The first-stage protocol matches the GX6605S-specific implementation in
[rva3/gx6605s-loader](https://github.com/rva3/gx6605s-loader): header
`59 00 08 00 00`, 8,188 bytes from artifact offset `0x20`, then `boot`.
The probe sends these 8,197 bytes only after the binary handshake, stops at
`RUNGET`, and never sends the second stage or a flash command. A partition
banner detected before transmission aborts the attempt. Success would leave
the bootstrap waiting in RAM, requiring a power cycle to resume normal flash
startup. Success has not yet been observed on this receiver.

Offline inspection identified clock initialization and accesses to UART base
`0x00403000`. Emulation stops at unmodeled clock/UART registers, so it does
not fully validate the bootstrap. The two consulted host implementations also
differ in second-stage packaging; that stage is excluded from this experiment.
Nine serial tests pass, including a pseudo-terminal transfer that checks the
exact first-stage bytes, `RUNGET` detection, absence of second-stage bytes,
raw capture, and terminal-setting restoration.

The first physical RAM-probe attempt is saved as
`traces/serial-ram-probe-20261113T181730652486Z.bin` and `.json`.
The host queued all 8,197 first-stage bytes at 18:18:00.060513Z after 81
received bytes. No `RUNGET` arrived. The receiver emitted mostly zeros and
undecodable bytes, then a second `00 58` prefix and `Partition Version`
approximately 15 seconds after transmission. The tool stopped with
`flash-startup-continued` after 6,209 received bytes. No second stage or flash
command was sent; receiver acceptance/execution of stage 1 is unproven.

This attempt was armed while the receiver was still powered, before asking
for an unplug/replug cycle. A power-off transient may have triggered the
first `00 58` match; the capture does not contain per-read timestamps to
establish this. Subsequent attempts must start with receiver DC power already
disconnected, then arm the probe, then reconnect power as a separate step.
The follow-up receive-only capture
`traces/serial-20261113T181905840926Z.bin` contains 65 bytes, including
two trailing `31 00 00 00` records matching the earlier normal runtime output.

The second RAM-probe attempt **succeeded**. With receiver power confirmed off
before arming, reconnecting power produced exactly `00 58 52 55 4E 47 45 54`
(`NUL`, `X`, `RUNGET`). The host sent the same 8,197-byte first stage at
18:20:15.900647Z and received `RUNGET` before stopping at 18:20:17.225713Z.
Evidence: `traces/serial-ram-probe-20261113T181954891262Z.bin` and `.json`.
This confirms the receiver input path, ROM upload handshake, and execution of
this first-stage bootstrap. The receiver is waiting for the second-stage
checksum/length/payload in RAM. No second stage or flash command was sent.
Power cycling restores normal flash startup. This is not yet an emulator
boot or a hardware register dump.

Static disassembly of the matching first stage gives a more precise
second-stage layout: routine `0x0010072C` receives checksum32 then length32,
includes all payload bytes in an additive checksum, discards the first
`0x2000` bytes, copies the next 32 bytes to virtual `0x90100000`, then copies
the remaining `length - 0x2020` bytes to `0x93F40000`. On checksum success it
prints `O` and returns to the jump at `0x001000D2`, targeting `0x93F40000`.
The word at payload offset `0x201C` is also copied to SRAM `0x00100A04`.
This explains why leading `boot` versus `toob` alone need not affect execution
(both have the same additive sum and lie in the discarded prefix). It does
not establish the safety or automatic startup behavior of the larger vendor
monitor. A small auditable RAM diagnostic can use this layout without loading
that monitor's flash-writing command handlers.

### Custom RAM diagnostic executed on the receiver

`tools/serial_ram_diagnostic.py` now generates a 96-byte ABIv1 program at
virtual entry `0x93F40000`. It reads CR0/CR17/CR18/CR30 into its own RAM buffer,
transmits a fixed 40-byte framed reply through the already initialized UART
at `0xA0403000`, then loops. It uses no stack, has no command parser, and
does not access SPI flash. Its host wrapper is 8,328 bytes including the
discarded 8 KB prefix, 32 metadata bytes, checksum, and length. It does not
include the vendor's second-stage monitor or its flash-writing handlers.

Before opening the real port, the tool executes the unchanged pinned first
stage from `0x00100760` through its checksum/copy loop and our diagnostic in
the interpreter (314,272 steps). This fixture supplies the established
post-`RUNGET` CPU/stack/MMU state and modeled UART/RAM. It excludes earlier
clock/DDR startup and real serial timing. The existing bootstrap epilogue's
write of 1 to OTP controller register `0x00F80004` is modeled separately;
the custom diagnostic writes only its own RAM buffer and the UART.

The physical test **succeeded**. Evidence:
`traces/serial-ram-diagnostic-20261113T182829084680Z.bin` and `.json`.
The receiver acknowledged the payload checksum with `O` and returned a
complete `STAR-RAM1` / `END-RAM1` framed response. Measured values:

| Register | Physical receiver, after RAM bootstrap |
| --- | --- |
| CR0 / PSR | `0x8F000150` |
| CR17 | `0x00000003` |
| CR18 | `0x0000007D` |
| CR30 | `0x0000001C` |

These are post-bootstrap measurements, not reset values. The fixture reports
`0x80000150`, `0x33`, `0xFD`, and `0` respectively. The discrepancy identifies
CPU control/readback behavior and initial state that require investigation;
it does not justify replacing reset values or adding blanket masks. The
fixture still validates the byte transfer, checksum, RAM placement, and
diagnostic instruction path.

Thirteen serial tests pass, including execution of the diagnostic's real
machine instructions, UART polling without premature output, exact transport
bytes, reply framing, and refusal to reuse a stage-1 continuation report.
The physical receiver is now in the diagnostic's final loop. Restarting it
is required to load another RAM program. A read-only flash-dump diagnostic is
the next step; no installed firmware dump or successful emulator kernel/menu
boot has been established yet.

### Read-only flash reader prepared

`tools/serial_flash_dump.py` generates a 1,432-byte RAM reader (9,664 bytes
including the stage-2 transport wrapper). Its only flash commands are `9F`
(read JEDEC identity) and `03` (read from address zero). The controller setup
matches the local firmware's `0x8420C002` sequence with the slowest SPBR
divider selected (`0x8420C01F`). Register fields and big-endian FIFO packing
also match the xboot GX6605S SPI reference. It writes only its own RAM,
SPI CTRL/TX FIFO, and UART data registers; it does not send WREN, program,
erase, or status-register-write commands. No vendor stage-2 monitor is loaded.

The program requires JEDEC `C8 40 16` before reading data. This identity is
listed for GD25Q32C in the
[GigaDevice datasheet](https://download.gigadevice.com/Datasheet/DS-00088-GD25Q32C-Rev4.1.pdf).
It reads a fixed 4 MiB into RAM at `0x90000000`, computes CRC32 in guest code,
deselects flash, transmits a framed copy over UART, then loops. Unexpected
identity or a bounded SPI polling timeout produces an error frame with no
dump body. Host parsing requires the exact header, length, footer, and a
matching independently calculated CRC32. SHA256 is recorded for a successful
capture; the raw flash image is saved separately as `.flash.bin`.

The unchanged pinned stage-1 receiver loop plus a 4,096-byte simulated NOR
fixture executes successfully in 453,573 interpreted steps. The fixture
returns the exact image and matching CRC32 and observes only `9F` and `03`,
with flash deselected at completion. Eighteen serial tests pass, covering
machine-code execution, byte order, CRC, unknown identity, SPI timeout,
truncated/corrupt frames, and the prior capture/diagnostic boundaries.
Real pinmux, clocks, electrical timing, and flash-read operation remain to
be tested on the receiver. No physical flash dump has yet succeeded.

Start with receiver DC power already disconnected. The CLI first completes
its local check, then arms the first-stage probe. Reconnect power only after
ARMED. After `RUNGET`, it automatically sends the fixed flash reader and
captures up to 4 MiB with a 900-second receive deadline. At 115200 baud the
data transfer alone takes approximately six minutes.

### Physical full-flash backup completed

The first physical flash-reader attempt **succeeded**. It received `RUNGET`,
executed our fixed RAM reader, identified the chip as **C8 40 16**, and copied
all **4,194,304 bytes**. SPI control/status before the reader's setup were
`0x00290002` / `0`. The host CRC32 matched the CRC32 calculated by the guest
over its RAM copy: **54E97C96**. The SHA256 is
`728a722369f5aa5fd3bcd1f81635cf1720b5abeff6a1b898e1da68865853f5a1`.
No flash-writing command was sent.

Artifacts:

- `traces/serial-flash-dump-20261113T184136405587Z.flash.bin`: raw 4 MiB flash
  image, starting at flash address zero, without a USB update header.
- Same prefix `.bin`: full UART frame including acknowledgment, header,
  image and footer; `.json`: transfer/checksum metadata.
- Same prefix `-stage1.bin` / `-stage1.json`: successful first-stage handshake.
- `analysis/receiver-flash-report.json`: partition and filesystem inspection.
- `analysis/receiver-filesystem.cramfs`: extracted ROOT filesystem image.

Independent consistency checks: the table's stored CRC32 **5FA826B5** equals
CRC32 of its first 508 bytes; BOOT used-region CRC32 **C5DB3874** and KERNEL
used-region CRC32 **08D0F20A** match the earlier physical startup log. ROOT's
CramFS internal CRC32 **421D8DDA** is valid. CRC32 of ROOT's 413,696-byte used
region is **2D11001D**, differing from the boot log's displayed **0E6E3C89**;
that displayed CRC's coverage/meaning remains unresolved. The completed
whole-image transfer checksum and CramFS internal checksum both pass.

The untouched V3.93 USB update still has SHA256
`10c0962a5f1fef6a5d0313e5e795dd376b9148cc4221696db9c2e99471b4d5b3`.
It is distinct from this backup. The application software version installed
on the receiver remains unknown; the physical bootloader is GxLoader v1.9.

Running the completed backup in the current emulator with `--offset 0`
executes **967,510 instructions**, emits `RUN`, and stops at PC **0x93F6CAC2**
on a read of unmodeled physical register **0x040036D4**. Report:
`traces/receiver-flash-emulator.json`. Its initial firmware sequence performs
read/modify/write configuration accesses to this register; no reset value has
been assumed to bypass the stop. This is additional hardware behavior to
investigate, not successful kernel/menu boot. The physical receiver remains
in our RAM reader's final loop and can return to normal startup with a power
cycle. No serial capture is running after completion.

### Fixed read-only register probe prepared

`tools/serial_register_probe.py` generates a 1,856-byte RAM program (10,088
bytes with stage-2 framing). It first reads physical `0x040036D4`, the
installed bootloader's current stop, then reads the 32 aligned words in
`0x04800140..0x048001BC` used by the V3.93 decoder. Those are the complete
fixed target list; no arbitrary address/read/write command parser is present.
It never writes a target register or accesses SPI flash. Its own writes are
limited to its RAM buffer and the UART data register.

Each 20-byte record contains a marker, physical address, observed value,
CRC32 of address/value, and closing marker. The next address is sent before
the hardware read; completed records remain independently checkable if a
later read does not finish. An overall header/footer and exact order/length
are required for complete success. Partial records are never assigned a
fabricated value. The complete UART frame is 685 bytes including the initial
stage-1 acknowledgment.

These will be observations after the pinned RAM bootstrap, with no target
configuration writes by this probe. They are **not** measurements at the
native firmware's exact read boundary or established reset values. In
particular, V3.93 configures some VPU words before reading the hash input;
a cold observation alone does not establish those later readback semantics.

Twenty-one serial tests pass, including the probe's real machine instructions,
exact single-read target order, absence of target writes, per-record CRCs,
corrupt frame refusal, and partial capture handling. The pinned unchanged
stage-1 receiver plus synthetic target fixture executes in 359,054 steps and
returns every supplied value correctly. Hardware read completion and actual
values remain unverified until the next physical capture.

### Physical register capture and decoder validation completed

The register probe **succeeded**. All 33 records passed their individual
CRC32 checks; the complete frame is 685 bytes. Evidence:
`traces/serial-register-probe-20261113T185427071506Z.bin`, `.json`, and
`.observations.json`. Physical `0x040036D4` read zero. Nonzero VPU observations:

| Address | Value |
| --- | --- |
| `0x04800148`, `158`, `168`, `178` | `0x00010000` each |
| `0x0480014C` | `0x80000000` |
| `0x04800180` | `0x32110101` |
| `0x04800190` | `0xE3000000` |
| `0x04800198` | `0x024002D0` |
| `0x0480019C` | `0x10001000` |
| `0x048001A8` | `0x00408040` |
| `0x048001B0` | `0x00000168` |

The remaining sampled words read zero. No target-register write or flash
command was sent. The receiver remains in this RAM program's final loop and
can return to normal flash startup with a power cycle. No physical serial tool
is currently running.

Two offline candidates were tested against the unchanged V3.93 guest decoder:
the raw observed window, and the observed window preserving its four earlier
configuration writes. The raw-window substitution failed the decoded size
check. **Preserving firmware writes decoded a valid ROMFS**, including its
superblock checksum. The first 200-million-instruction follow-up window was
insufficient; extending that window to two billion reached the validation
boundary. The probe now distinguishes an unfinished budget from an invalid
ROMFS and can export the first valid result to a new file. No guest check or
instruction was patched.

Evidence: `traces/observed-vpu-decoder-probe-extended.json` and
`analysis/vega-v393-decoded.romfs`. Its file named `ecos.bin.gz` actually holds
an LZMA-alone stream, not gzip. A standard-library LZMA decode consumed that
stream completely and produced a 5,222,504-byte kernel:

- `analysis/vega-v393-ecos.lzma`: 2,402,573 compressed bytes.
- `analysis/vega-v393-ecos.bin`: kernel SHA256
  `d9fa9e32daaf57516428d5982f6b7cbd962d312488d08e8693d519f784429eb3`.
- `analysis/vega-v393-decoded-kernel.json`: sizes, hashes and extraction context.

### Receiver-observation profile and first kernel execution

`receiver-observed` is now a separate bundled profile, selected by the native
app by default. It supplies the measured VPU baseline, provisional storage for
the sampled `0x040036D4` configuration word, and measured flash identity
`C8 40 16`. Measurements were taken after the RAM bootstrap; using them as
initial values and modeling configuration writes as storage remains an explicit
assumption. The older strict/exploratory profiles retain their behavior.

Raw flash and USB update images now select their payload offsets automatically
(`0` and `0x100` respectively); explicit `--offset` still overrides detection.
The native app chooses the captured backup when available, supports the new
profile, and sends execution in 10,000-instruction chunks for lower overhead
while retaining pause/step behavior.

The receiver backup with this profile prints the physical bootloader's table,
GD25Q32 identity, and **`Decompress kernel ...ok`**, then executes the
decompressed kernel. A dual count-up timer model implements documented
reset/start, enable, divider, initial count, overflow status, and W1C clearing.
It is enabled only in the new profile. Its 27 MHz input and virtual-time scale
are assumptions; pending IRQs are recorded but CPU interrupt delivery is not
implemented. The kernel programs divider 26 and enables the event timer.

A fresh complete run reaches PC **`0x903AE25C`** after **2,473,977,591**
instructions and stops on a write of `0x00420042` to physical **`0x00205008`**.
This is the GX I2C clock register, still unmodeled; tuner/slave devices and
interrupt delivery also remain incomplete. Report:
`traces/receiver-observed-kernel.json`. This confirms execution into the
kernel, not successful receiver-menu startup. Ninety-seven tests pass,
including timer sequences, profile identity, raw/update detection and the
native/Python instruction differential checks; the bridge test also passes
after increasing execution chunks.

### I2C and kernel UART bring-up continued

The observed eCos device table and initialization sequence contain three I2C
controller instances at `0x00203000`, `0x00205000`, and `0x00206000`. Their
control/data/clock registers now have a synchronous model following xboot's
documented DONE and WACK polarity. All virtual buses are empty: address/data
operations report NACK, STOP can complete independently, and no tuner/EEPROM
response is invented. Interrupt delivery and transfer timing remain absent.
Sparse device matching preserves the CIR block between I2C instances.

The kernel also writes `1728` to UART extension word `0x00400014`. This
specific observed configuration access now provides storage/readback; an
unwritten read still stops rather than inventing a reset value. Its electrical
effect is unmodeled, as with the other UART configuration words.

A fresh complete backup run now reaches **2,474,071,601 instructions**, PC
**`0x9012C028`**, and stops reading **`0x04E3FFC0`**. Evidence:
`traces/receiver-observed-kernel-latest.json`. The guest computes this address
as `0xA4E00000 + ((0xFF << 8 | 0xF0) << 2)` in a banked register-access
routine; its caller checks the returned byte while identifying the demodulator.
No value has been substituted to pass that check. Kernel decompression,
timer setup, three I2C clock configurations and UART extension setup complete.
The latest complete regression suite has **103 passing tests**.

The fixed register probe now has a second preset, `--group demod-id`, for
16 aligned words `0x04E3FFC0..0x04E3FFFC`. Its 1,516-byte RAM program uses
the same per-record CRC framing and makes no target-register writes or flash
accesses. The unchanged bootstrap and synthetic fixture pass in 346,661 steps;
tests verify exact read order and refusal of arbitrary addresses. Actual
post-bootstrap values and hardware read completion remain to be measured.

### Native app 0.4.0 and next measurement

The rebuilt native app passed its window/backend smoke test with the captured
backup and receiver-observed profile: 100,000,000 guest instructions, expected
bounded-budget stop, and the physical bootloader's table/identity output.
Summary: `traces/native-app-smoke-20261113.json`. This checks app plumbing;
the longer CLI runs establish kernel execution and the current device boundary.

A fresh V3.93 run with timer/I2C/UART support also executes its decompressed
kernel. It stops after **3,984,342,873 instructions**, PC **`0x901AF214`**, on
an unmodeled read of **`0x04600120`**. Report:
`traces/vega-v393-observed-kernel-latest.json`. No value has been invented for
that register. The original update and the full receiver backup still match
their recorded SHA256 hashes; physical flash has not been programmed.

The next fixed register preset, `--group kernel-reads`, reads the installed
firmware's first demodulator-ID word, the V3.93 read at `0x04600120`, then the
remaining 15 demodulator-ID words. The program is 1,536 bytes (9,768 bytes with
the transport wrapper). It passes the unchanged stage-1 simulation and all
per-record checks in 347,390 steps; the targeted register-probe tests pass.
Its observations will still be post-RAM-bootstrap values, not automatically
the same state as the native kernel's access. No target writes or flash
commands are present. Receiver DC power must be removed before arming, then
reconnected only after ARMED. This next preset has not been sent to hardware.

### Kernel-register observations captured

The `kernel-reads` preset **succeeded**, returning all 17 CRC-checked records.
Evidence: `traces/serial-register-probe-20261113T211023311259Z.bin`, `.json`,
and `.observations.json`. The demodulator bank's first values are word-lane
replications: `0x04E3FFC0 = 0x04040404`, `FFC4 = 0x4C4C4C4C`,
`FFC8 = 0x80808080`, `FFCC = 0x30303030`, `FFD0 = 0x2E2E2E2E`,
and `FFF8 = 0x01010101`; the other sampled bank words read zero.
`0x04600120` read zero. The low-byte pair `04 4C` is consistent with the
`0x044C` constant checked by a guest identification path; complete demodulator
attachment has not been established. No target writes or flash commands were
sent. These remain post-RAM-bootstrap observations.

The bundled receiver-observed profile now includes those values as provisional
initial register data. Retaining subsequent configuration writes as storage
does not model all device dynamics or write masks. In the V3.93 path,
`0x04600120` is subsequently written with `0x40000`; the real side effects
of that operation remain unmodeled.

Continuation of the verified-backup execution now reaches **2,474,132,504
instructions**, PC **`0x9012C028`**, and stops reading **`0x04E3FF44`**, the
same demodulator bank at register `D1`. Evidence:
`traces/receiver-kernel-id-probe.json`. The V3.93 observation-profile run
reaches **3,984,344,826 instructions**, PC **`0x901B2AA4`**, and stops reading
**`0x04A0000C`**; report `traces/vega-v393-kernel-id-probe.json`. These stops
have not been hidden with invented status or identity values.

### Read-only resident register monitor prepared

`tools/serial_register_monitor.py` now generates a 1,400-byte RAM monitor
(9,632 bytes with stage-2 framing). It remains in RAM and accepts only
CRC-checked aligned word-read requests in these fixed physical windows:

- `0x04E00000..0x04E3FFFC` (observed banked demodulator interface).
- `0x04600000..0x04600FFC` (window containing the V3.93 configuration access).
- `0x04800000..0x04800FFC` (VPU register window).
- `0x04A00000..0x04A00FFC` (window containing the V3.93 kernel's next read).
- The single previously sampled word `0x040036D4`.

It has no target-write, flash, memory-upload or execute command. `RREG`
requests carry address, transaction ID and CRC32; `RVAL`/`RERR` replies echo
the address/transaction with their own CRC32. Guest code rejects malformed
commands, CRC failures, unaligned addresses and all addresses outside the
fixed windows before performing a target access. Host verification rejects
stale/wrong-address replies, and requires the matching successful monitor-load
report and program hash. Closing/reopening the host serial port permits more
queries without replacing the RAM program. A receiver power cycle exits it.

Hardware reads can still fail to complete; the host records a timeout rather
than fabricating data. There is no blind retry/resynchronization and no reset
command. Capture metadata labels all observations as post-bootstrap reads,
not native-kernel/reset values. The monitor changes only its private RAM and
UART data; the existing first-stage epilogue still performs its separately
documented OTP-controller configuration write.

The current **109-test suite passes**. Monitor tests execute its real machine
code, exercise repeated queries and all window boundaries, reject flash/write
requests and damaged/stale packets, and verify host byte counts, captures and
terminal restoration using a pseudo-terminal. The unchanged pinned first
stage reaches the monitor's ready loop in 338,749 modeled steps. This monitor
has not yet been uploaded to the physical receiver.

The Mac app is rebuilt as **0.4.1** with the newer measured profile data.
Its native frontend/backend smoke test passed at 100,000,000 instructions;
actual captured output is in `traces/native-app-smoke-20261113-v041.json`.
The full receiver backup and original V3.93 update still match their recorded
SHA256 hashes. The receiver has been confirmed powered off before arming the
resident-monitor upload; it has not been sent a query while running native
firmware or while in the earlier one-shot probe.


### Resident monitor captured and both kernels advanced

The resident monitor loaded successfully. Its ready capture is
`traces/serial-register-monitor-20261113T212918493330Z.json` / `.bin`.
Closing and reopening the Mac serial connection preserved the query loop.
The 256 aligned words at `0x04E3FC00..0x04E3FFFC` were read with per-query
CRC32, echoed address and transaction verification; evidence is
`traces/resident-demod-bankff-20261113T213152583773Z.json` / `.bin`.
`traces/resident-v393-next-20261113T213347940836Z.json` records
`0x04A0000C = 0x76543210`. No target-register writes were requested.

The receiver-observed profile includes these samples as provisional initial
values. Existing guest writes are retained when continuing a saved execution.
These observations do not establish reset values, device dynamics, or write
masks. An unsampled neighboring demodulator bank still stops explicitly.

Both kernel continuations now stop reading `0x04C00000`: the backup after
2,474,273,330 instructions at PC `0x90170D3A`, and V3.93 after 3,984,344,870
at PC `0x901B2AFC`. Reports are `traces/receiver-resident-baseline-probe.json`
and `traces/v393-resident-baseline-probe.json`. This value remains unknown.

The monitor source adds only the next kernel-accessed register page,
`0x04C00000..0x04C00FFC`, to its explicit read whitelist. It is now 1,420
bytes (9,652 bytes framed), passes the query-validation tests, and reaches
its ready loop through the unchanged first stage in 339,129 modeled steps.
The updated program must be loaded after removing DC power; the earlier
monitor cannot read this added page. Reads can still have device side effects
or stall: the page is not claimed to have fully documented register semantics.
No target-write or flash-programming command has been added.


The app is rebuilt as **0.4.2** with these additional verified samples.
All 109 tests pass, including unknown-neighbor rejection and guest-write
retention. Its native frontend/backend smoke test passed at 100,000,000
instructions; `traces/native-app-smoke-20261113-v042.json` records the result.
Both firmware files retain their original SHA256 hashes. This app update
records the later initialization boundary; it still does not display menus.


### Configuration reads captured; byte-width monitor prepared, 2026-11-14

The extended first monitor loaded successfully, with ready report
`traces/serial-register-monitor-20261113T214452730876Z.json`. Its subsequent
query report `traces/serial-register-monitor-20261113T214818823013Z.json`
verifies `0x04C00000 = 0xC8000000` and `0x04C00004 = 0x00100C01`.
The bundled receiver-observed profile now includes these two initial-value
observations. The backup continues to `0x040036C0` at PC `0x9017799C`,
2,474,313,228 instructions; V3.93 continues to a one-byte read at
`0x04F10004`, PC `0x90198FEE`, 3,984,344,965 instructions. Evidence:
`traces/receiver-resident-page4c-probe.json` and
`traces/v393-resident-page4c-probe.json`. Neither next value is measured yet.

The new resident protocol identifies itself as `STAR-MON2` and accepts
`RBYT`, `RHAL` and `RREG` requests for naturally aligned 1-, 2- and 4-byte
reads, respectively. All use the existing 16-byte request / 20-byte response
framing with address, transaction ID and CRC checks. The load instruction
uses the requested width; it does not widen a peripheral byte access to a
word. Its fixed allowed aperture is `0x04000000..0x04FFFFFF`, covering the
peripheral banks accessed by these kernels. This does not establish that
all addresses are implemented or lack read side effects. Query specific
firmware-accessed registers; do not scan the entire aperture. RAM, OTP,
flash-controller and UART-monitor addresses remain outside this aperture.
There is no target-write, upload, execute or flash-programming command.

The 1,372-byte program (9,604-byte transport packet) reaches its ready loop
through the unchanged pinned first stage in 338,217 modeled instructions.
Tests execute actual byte/halfword/word loads and exercise width/alignment,
window limits, command/CRC rejection and pseudo-terminal transport.
An attempt to load this program timed out waiting for power-on:
`traces/serial-register-monitor-20261113T215404484523Z-stage1.json` records
**zero bytes received and zero bytes sent**. It is not a successful load.
The next attempt must be armed with DC power already removed. Arming may
now wait up to two hours, avoiding the earlier ten-minute deadline.

Custom provisional register entries accept an optional `size` of 1, 2 or 4
bytes (default 4). The bus requires that exact access width and refuses
unsampled adjacent addresses. A `reset` of `null` means the initial value
is unknown: a read stops explicitly until a guest write initializes the
entry. This permits explicit configuration storage without inventing a
zero reset value. Entries with overlapping address ranges are rejected.
No new device-ready, acknowledge, authentication or interrupt response is
synthesized by this storage facility. Hardware side effects remain unmodeled.


### Byte-width resident monitor loaded and further kernel tracing

`STAR-MON2` loaded successfully. The ready report is
`traces/serial-register-monitor-20261113T220819023561Z.json` / `.bin`,
and its initial three word replies match earlier captures. This is distinct
from the earlier timed-out attempt, which sent nothing. The monitor remains
resident after the host serial connection closes.

Individual reports `traces/resident-kernel-read-20261113T2213*.json` and
`traces/resident-kernel-query-20261113T2213*.json` / `2216*.json` verify these
post-bootstrap observations with CRC32, echoed address and transaction IDs:

| Address | Width | Value |
| --- | --- | --- |
| `0x040036C0` | 4 | `0xC0010257` |
| `0x04003594` | 4 | `0x00010257` |
| `0x04003508` | 4 | `0x0000333F` |
| `0x040036E8` | 4 | `0x00000001` |
| `0x04F10004` | 1 | `0x00` |
| `0x04A00000` | 4 | `0x00100008` |
| `0x04A00004` | 4 | `0x00000C01` |
| `0x04A00008` | 4 | `0x00000007` |
| `0x04A00014` | 4 | `0x00000004` |

The exact peripheral byte read succeeds physically; no widened word read
was substituted. These are provisional initial data, not measurements at
the native kernel's access times. The profile records that limitation.

The installed kernel at `0x90177950` writes all ones to `0x0400350C`, clears
`0x04001B00..0x04002FF8` in a word loop, then writes zero to `0x0400350C`
and `0x04003504`. Other traced writes initialize `0x040032F4`, `32F0`,
`36E0`, `36E4`, `0x04700014` and V3.93's `0x04A00010`. The observation
profile now explicitly records these as **opaque writes** with `reset: null`
and `readback: false`. No physical target writes were requested. The emulator
retains the last written value for diagnostics but **always stops on a read**
of such an entry. Even a guest write of zero/all ones cannot manufacture a
status reply. Device behavior, interrupts and command effects remain absent.
This is a tracing approximation, not evidence those devices work.

Both diagnostic continuations reach the read of `0x0030A130`: backup
2,474,428,143 instructions / PC `0x903ACBCA`, V3.93 3,984,378,876 /
PC `0x903A02AA`. Latest snapshots and journals are
`traces/receiver-monitor2-progress.json` and `traces/v393-monitor2-progress.json`.
The GCTL model currently rejects offset `0x130`; its reset and semantics
are still unknown. The value has not been set to zero or inferred from
neighboring registers.

A tested `STAR-MON3` reader adds fixed windows for I2C/CIR, timers/watchdog,
GPIO, GCTL, interrupt routing, EHCI/OHCI and DRAM-controller registers to
the existing AV aperture. SPI, both UART interfaces, OTP and memory remain
excluded. These addresses follow the existing SoC maps listed in references;
that does not establish every offset's semantics or safe read behavior.
Queries remain limited to actual missing firmware accesses. It is 1,552
bytes (9,784 framed), with the unchanged first-stage continuation reaching
its ready loop in 341,637 modeled instructions. It has not yet been loaded;
another power-off/arm/reconnect sequence is needed to read the GCTL word.


## Scheduler, video timing and separate flash-bank capture, 2026-11-14

STAR-MON3 loaded successfully after the preceding section. Its first successful
ready report is `traces/serial-register-monitor-20261113T222347895983Z.json`.
After the security capture it was restored successfully in
`traces/serial-register-monitor-20261114T0020.json`; it currently remains in
RAM waiting for bounded CRC-checked register reads. Keep the receiver powered
on to use that monitor. A normal power cycle exits it.

### Implemented functional behavior

- ABIv1 IDLY4, PSR register-bank switching, IRQ entry and RTE allow timer 0
  on interrupt line 10/autovector 10 to run the kernel scheduler. Native
  instruction batches stop at timer deadlines. Other IRQ sources are absent.
- Timers reload the initial-count word at overflow; this interpretation follows
  the public timer references and the receiver ISR, with no physical counter
  waveform verification. RTC seconds/minutes/hours use packed BCD, while
  milliseconds remain binary, as required by both native kernels.
- The observed profile advances time at 10 million instructions per second.
  This is a functional scheduling assumption, not a cycle-accurate 594 MHz CPU.
- EHCI and both OHCI controllers implement measured capability/reset values,
  frame timing, status W1C and empty-port commands. No USB device/transfer DMA
  or USB CPU IRQ delivery is implemented. Transfer-list commands stop.
- `resident-vpu-timing-20261113T232203620733Z.json` contains 48 verified samples:
  SYS_PARA is consistently `0x80000001`; SCAN_LINE advances and alternates bit 11.
  The bootstrap raster is represented as 625-line interlaced PAL. VOUT's
  measured baseline is `0x41F00000`; guest `0x60009` selects 576p50 and
  `0x60026` selects 1080i50 using the public format table. Progressive bit-11
  frame parity is inferred from kernel calibration, not physically sampled.
- SPI TX packing latches the length at the FIFO write. A later shorter transfer
  clocks that prefix; the written word remains available for another transfer.
  This handles the kernel's `9F000000`/one-byte/four-byte identity sequence and
  preserves the physical RAM reader's byte/word packing. Byte FIFO access works.
- OSD software scanout now supports nearest-neighbor upscaling. It renders
  guest RAM only; filter phases, GA/2D command execution and video decoding
  remain absent. Both captured guest frames are blank.

Physical USB baselines are in `resident-ohci-baseline-20261113T2232*.json` and
`serial-register-monitor-20261113T222653562781Z.json` / `222805013816Z.json`.
Further CRC-checked initial GCTL words are recorded in the profile. The native
kernel's 12-byte chip-word copy reads `0x0030A190/194/198`; these were captured
in `resident-chip-registers-20261114T0021.json`, with no substituted identity.

### The main flash backup did not include the security bank

Both kernels requested command **48H at 0x003000**. GigaDevice's GD25Q32C
specification identifies that as a separate security-register read, outside
the normal 4 MiB address array. The emulator stopped with
`missing-flash-security-data` rather than returning synthetic bytes.

The new `tools/serial_flash_dump.py --security-bank 0x3000` option loads a
fixed 1,444-byte RAM reader (9,676-byte framed transport), verifies JEDEC
`C84016`, and reads only that 1,024-byte bank. Its only flash opcodes are 9F
and 48. Offline execution verified exact bytes, CRC and chip deselection;
`traces/security-reader-offline-20261114.json` records the pinned-stage-1 test.
No write-enable, program, erase, lock or status-write command is sent to the
physical flash. The original generic RAM report's `flash_commands_sent:false`
refers to direct host commands; this diagnostic does issue those two read
opcodes from RAM. New reports make that distinction explicit.

Successful physical report: `traces/serial-security-bank-20261114T0010.json`.
The separate copy is `traces/serial-security-bank-20261114T0010.security-3000.bin`:

- Size: 1,024 bytes; CRC32: `E3D726E6`.
- SHA256: `9db32f2d1a7205f19659ab39b39ab77a50b33a48c0de4234ed1191e5822d9f8b`.

The observed profile includes the exact captured bank as `spi_security`.
Other security banks remain unknown and stop on access. Profile summaries show
bank size/hash, rather than contents. Security-bank programming remains absent.

### Guest writes stay in virtual flash

The receiver-backup kernel subsequently requested WREN and a page program.
`spi_writable:true` explicitly enables an in-memory overlay for guest WREN,
WRDI, 256-byte page program and 4 KiB sector erase. WEL is required, page
addresses wrap within 256 bytes, programming only clears bits, and successful
mutations clear WEL. Completion is immediate; protection/status-write commands
remain unsupported. The main-array bytes loaded from disk stay immutable.
Reset creates a new overlay; no file or physical receiver write occurs.
The read-only capture fixture still rejects all mutation commands.

### Validation and remaining boundary

The suite now includes 155 tests covering CPU/IRQ context, timer deadlines,
USB reset/status, shared virtual clocks, raster mode changes, BCD time, security
capture framing and volatile flash isolation. Native and Python instruction
paths remain independently checked by the existing differential fixtures.
No working receiver menu has been demonstrated.

Fresh boots are recorded separately from migrated diagnostic continuations:
`traces/receiver-observed-cold-latest-20261114.json` and
`traces/v393-observed-cold-latest-20261114.json`, with subsequent cold-machine
continuations in `traces/receiver-cold-progress-20261114.json` and
`traces/v393-cold-progress-20261114.json`. The fresh receiver backup reached
GCTL `0x190` after 2,517,992,747 instructions before that word was captured.
V3.93's fresh run encountered many finite I2C NACK polling timeouts because
no virtual slaves are attached. It is slower than the previously saved
continuation. An absent tuner is never changed to ACK just to speed startup.

Latest continuation frames establish enabled scanout only. GA command buffers
are queued through `0x04700004/08`, but command DMA and blit semantics have not
been implemented. Blank pixels therefore cannot establish normal application
rendering. Remote-key injection, other IRQs, tuner/media engines and a complete
cold-boot comparison still require work.


The fresh-machine backup continuation reproduces enabled scanout without
importing the earlier migrated kernel state. The backup reaches
2,917,992,747 instructions, PC `0x901534D4`, with 4,116 timer IRQs and its
720×576 blank frame. V3.93 reaches 4,085,000,000 instructions, PC `0x901CCA26`;
its final display and IRQ count are in `v393-cold-progress-20261114.json`.
The backup cold frame artifact is `receiver-cold-guest-osd-20261114.png`.
V3.93 is still initializing at that checkpoint. This confirms the I2C wait was finite;
it does not establish working menus or graphics command execution.


V3.93's final cold-machine continuation reaches **4,485,000,000 instructions**,
PC `0x901CC924`, with **4,677 timer IRQs**, and produces the same blank
1920×1080 guest OSD frame as the earlier diagnostic continuation. The artifact
is `traces/v393-cold-guest-osd-20261114.png`. Both images therefore reproduce
enabled scanout from cold-machine execution; neither demonstrates a menu.
`traces/graphics-command-state-20261114.json` records the pending 80-byte guest
command buffers and opaque GA register writes for the next implementation step.
No blit was executed or fabricated by this capture.

The rebuilt **Mac app 0.4.5** passes its native frontend/backend 100-million-
instruction smoke check (`traces/native-app-smoke-20261114-v045.json`). All
**155 tests pass**. Input SHA256 values remain identical to the originals
listed above. The resident register monitor is ready; no further power cycle
is needed to query it.

## 2026-11-14: measured GA copy, first firmware dialog, OTP boundary

This supersedes the blank-frame and resident-monitor status immediately above.
The monitor has been replaced by fixed RAM diagnostics and is no longer
available for register queries. The packaged Mac app remains **0.4.5**; the
following graphics changes are currently diagnostic source continuations.

The physical GA experiment proves an unscaled 4×3 RGBA copy, including source
alpha, eight-pixel pitch, unchanged padding and guards. Completion is bit 4
of GA +0x1C; write 0x103 acknowledges it and reads back 0x103. Factory flash
ID was also captured. The later combined interrupt/bank experiment timed out
after its startup marker, so it supplies no register or bank observations.
The model's IRQ62 routing/level behavior follows the native handler and public
interrupt-controller driver and remains physically unverified.

A standalone read-only reader successfully captured all three 1024-byte
security banks. Its report is
`traces/serial-flash-dump-20261114T013210843939Z.json`, CRC32 **0e72b35b**;
bank 3 matches the earlier independent capture exactly. All bytes are now in
both observation profiles. No flash programming or erase commands were sent.

With those bytes and the measured copy mode, the receiver-backup continuation
executes three graphics jobs and displays its first real guest dialog:
**Start to Burn SN,don't power off**. See
`traces/receiver-first-graphics-20261114.png` and
`traces/receiver-graphics-progress-20261114.json`. At 2,555,346,284 instructions,
PC 0x900B82C8, the emulator stops on an OTP programming request. Its OTP bytes
are still synthetic zeros, so the firmware has selected serial provisioning.
That programming request is blocked; it was never sent to the receiver.

The next experiment is a fixed read-only 2048-byte OTP capture following the
native READ protocol, with bounded polling, IRQ disabled and CRC over header
and data. Its programming bit and data field are never set. Local validation
passed through the unchanged stage-1 continuation in 1,086,179 instructions.
The emulator can load an explicit full capture through `otp_data` and reports
its hash instead of publishing the identity bytes in snapshots. OTP
programming remains unsupported. Physical capture is pending; no real OTP
bytes have been substituted yet. See `docs/graphics-command-notes.md`.

## 2026-11-14: OTP captured, passive capture session, coprocessor boundary

The OTP capture above is now complete. The report is
`traces/serial-otp-read-20261114T014908884759Z.json`: all **2048 bytes**
passed header/data CRC32 **0166a8a5**, SHA256
`00b172e697cdf7afab5cdfaf95e6a7ae7c8c7a95dc08a72883894da5997ed3fc`.
Auxiliary information is **0xD2**. The exact bytes and info are imported into
both observation profiles; programming remains blocked. The reader never set
the programming bit or data field. Source flash and OTP were not modified.

With captured identity data, a fresh receiver-backup boot gets past the
serial-provisioning decision. Its later cold-machine continuation reaches
**2,665,676,587 instructions**, PC **0x9016094A**, and stops on the
unimplemented coprocessor handshake at **0x04200160**. The fresh-machine
report is `traces/receiver-captured-otp-cold-20261114.json`.
V3.93's cold-machine continuation reaches **4,241,105,249 instructions**,
PC **0x901AA664**, at the same handshake; see
`traces/v393-captured-otp-cold-progress-20261114.json`.
Both have executed a graphics copy; neither has demonstrated a menu.

The read-only register monitor was restored in
`traces/serial-register-monitor-20261114T0157.json`. It remains in RAM and
available for further queries while the receiver stays powered on. One session
collected **52 additional CRC/transaction-verified replies covering 45 unique
registers**, including repeated mailbox samples and IRQ configuration.
Twenty-four are provisional initial/readback values; the mailbox and IRQ
baseline are separate evidence only. Named opaque AV initialization
writes record virtual writes but stop on subsequent readback. No physical
target-register writes were issued by these queries. Post-bootstrap samples
do not establish native reset values or device side effects.

The IRQ baseline in `traces/resident-intc-config-20261114T0222.json` has both
gates zero and both masks plus SOURCE0..15 all FFFFFFFF. This supports the
missing-routing explanation for the first GA experiment; it does not prove
the later timed-out experiment succeeded. These post-bootstrap configuration
words are not promoted to native resets in the existing functional controller.
Four extra +160 reads are stable at 94D94ACE in
`traces/resident-mailbox-intc-baseline-20261114T0220.json`.

`analysis/receiver-capture-index-20261114.json` indexes the raw flash,
security-bank, factory-ID, OTP, register and microcode artifacts. The two
4096-byte microcode uploads were extracted from guest RAM and have different
hashes. The native upload loops and current limits are documented in
`docs/av-microcode-notes.md`. The +160 observation **94D94ACE** is not used
to manufacture completion after the virtual upload/start command.

V3.93 also reads OTP configuration +4 and sets bit 0 before reading bytes.
The functional read-enable latch starts at 1, following the pinned bootstrap
initialization; its native cold reset is not independently measured. Only
bit 0 storage is supported there. Programming control/data fields still stop.
Guest 64 KiB block erase D8 is now supported in the volatile NOR overlay,
alongside 4 KiB erase and page program. No physical erase is sent, and the
source image stays immutable.

The native execution path batches exact iterations of the observed finite
I2C NACK polling loop on the explicitly empty virtual bus. It preserves the
counter, instruction count, shared time, RAM/CPU state, recent MMIO events and
instruction history, and stops before enabled timer deadlines and breakpoints.
The timeout exit still executes normally. Differential tests cover successful
status fallback, malformed loops and the RAM-to-MMIO boundary.

The Receiver observations source profile now enables measured RGBA copy and
uses the captured OTP, all security banks and factory ID. Mac app **0.4.6**
packages this behavior. A working menu, coprocessor execution and remote input
remain incomplete. Original image SHA256 values match the earlier records.

Final verification: **175 tests pass**. Both complete fresh-machine runs now
reach the same boundary: backup 2,665,676,587 instructions and V3.93
4,241,105,249, with their final PNG/JSON artifacts under
`traces/{receiver,v393}-captured-otp-cold-20261114.*`. Neither requires a
synthetic serial-provisioning outcome. The rebuilt **Mac app 0.4.6** passes
the native frontend/backend **100-million-instruction** smoke test in
`traces/native-app-smoke-20261114-v046.json`. That bounded smoke test verifies
the app execution path, not a working menu. The original V3.93 image and
physical flash backup hashes are unchanged. The resident read-only monitor
is still available; keep the receiver powered on for further queries.

## 2026-11-14: AV upload tracking and full-program correction

The optional `analysis/gx6605s-av-upload-profile.json` now uses a dedicated
AV MCU model instead of opaque register storage. It retains every indexed
halfword written through +04, rejects invalid uploads, and records the startup
configuration. Both unchanged native kernel continuations upload exactly
2048 words matching the saved IRAM images, then stop explicitly with
`unsupported-av-microcode`. Evidence:
`traces/receiver-av-upload-20261114.json` and
`traces/v393-av-upload-20261114.json`. No mailbox acknowledgment or instruction
execution is synthesized. The packaged app remains 0.4.6; this is an optional
source profile, not a new working-menu release.

The earlier analysis missed the full program in DRAM: each kernel prepares
**0x3000 bytes / 6144 halfwords** with reordered 64-bit groups, in addition to
the 4096-byte IRAM upload. Both full packed programs match guest DRAM exactly.
The backup's program is at physical 0x1263F000; V3.93's is at 0x12633000.
The first pages of the buffers were zero, but the complete buffers were not.
The claim that all AV buffers were empty was incorrect. +58's meaning also
remains unknown; its previous interrupt-mask label was unsupported.
`analysis/av-full-program-layout-20261114.json` records the corrected layout.
See `docs/av-microcode-notes.md` for the source pointers, hashes and packing.

An all-target GNU binutils 2.42 reference build was compared against the IRAM
prefix in both byte orders. No ISA identification was established. The
comparison is preserved in
`analysis/av-reference-decoder-comparison-20261114.json`; ordinary mnemonic
decodings alone do not establish the target architecture. The reference tool
is temporary and is not bundled into the emulator.

The fixed `tools/serial_av_startup_probe.py` experiment passed the pinned
stage-1 continuation check in **3,147,463 instructions** and then ran on the
physical receiver. `traces/serial-av-startup-20261114T082509334521Z.json` and
its raw CRC-verified frame establish that the **receiver-backup** AV program
cleared +160 after three polls. Every byte of the complete 0x90000-byte AV
DRAM window matches the prepared emulator state; no DRAM changes were found.
The capture SHA256 is
`9659eaa59e378b8099ba0a1c922f6d7973f49f45e6ab6eea31baf1ba738cd386`.
The raw snapshots and `analysis/av-startup-ram-diff-20261114.json` are saved.

The second, warm V3.93 attempt was **skipped**, because its initial +18 was
0x8B. It did not upload or execute V3.93; its identical RAM snapshot is still
the receiver program's state. Subsequent read-only queries in
`traces/serial-register-monitor-20261114T083012718413Z.json` find +18 stable
at 0x8B, control +00=0, and +20/+160/+164/+168/+170/+174 all zero. +18 may
relate to an MCU instruction position, but its identity is not established.
Other sampled words are not automatically treated as initialized registers.

An opt-in behavioral startup model is available through
`analysis/gx6605s-receiver-av-bootstrap-profile.json`. It checks all 2048
IRAM words, every startup configuration word and the SHA256 of the complete
0x90000-byte AV DRAM window before applying the observed startup transition.
It does not execute MCU instructions, alter DRAM, assert IRQ or complete later
commands. The measured stopped +18=8B also represents running idle status
as an explicit functional approximation; physical instruction-PC timing is
not modeled. V3.93 and mismatched inputs cannot use this transition.

With this model, the unmodified receiver kernel proceeds to its first channel
configuration write: **0x0420105C=10 at PC 0x90160DCA**, instruction count
**2,665,677,370**. It stops there because AV channel command processing is
not implemented. `traces/receiver-av-bootstrap-progress-20261114.json` records
the exact boundary. No new graphics job or working menu was demonstrated.
This remains a source-only experimental profile; packaged Mac app 0.4.6 is
unchanged.

The version-2 fixed trace adds a read of +18 before stopping the core and can
put V3.93 first, so it receives an independent cold startup rather than an
invalid warm retry. The version-1 packet is byte-for-byte unchanged and the
current resident monitor remains valid. Both versions stop the core before
copying RAM and hand off to the same read-only monitor; no new hardware write
addresses or commands are introduced. The next fixed trace has passed local checks; the loader is not armed while
waiting for confirmation that receiver DC power is unplugged.

Validation after these changes: **185 tests pass**, including physical-capture
CRC/framing, unchanged version-1 packet SHA256, both image-order fixtures,
version-2 status metadata, full AV RAM bounds and refusal to acknowledge
changed startup inputs or subsequent commands. The version-2 pinned-stage-1
continuation passes in **3,147,475 instructions**. Its full packet SHA256 is
`90961b39130c21d7acf0b4fff4d6da952a86e456c0671468a54b189e16b2cb11`.
The user has been asked to unplug DC before the next loader is armed.

A complete **fresh-machine boot** with the optional bootstrap profile also
reaches exactly **2,665,677,370 instructions / PC 0x90160DCA** and stops on
0x0420105C=10. This agrees with the saved continuation, including one graphics
copy and no demonstrated menu. Evidence:
`traces/receiver-av-bootstrap-cold-20261114.json`. The original update and
physical flash backup SHA256 values remain unchanged. No native library or
packaged app was rebuilt while the user's app was running.
