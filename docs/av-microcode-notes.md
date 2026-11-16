# Coprocessor startup boundary, 2026-11-14

Both firmware versions now reach a wait on physical **0x04200160** after
graphics initialization and checks using captured OTP/security/flash identity
data. This is not another missing constant that can establish command
completion. The virtual word remains explicitly opaque after guest writes.

The receiver-backup initialization function configures DRAM addresses at
0x04200100/104/108, writes setup words at +10C/+110/+170, and tests +18.
The physical monitor reads +18 as zero. The native loop at 0x90160908
then uploads **2048 halfwords** from guest address 0x904E4080 through +04:
each 32-bit write is `(index << 16) | instruction_halfword`. It writes 1
to +160, sets +58 configuration, writes 1 to +00, then waits for +160 to become
zero at 0x9016094A. The first buffer includes a separately prepared program
image; the earlier claim that all configured DRAM buffers were zero was
incorrect and was based on their initial pages rather than their full extent.

`analysis/receiver-av-microcode.bin` preserves the exact 4096-byte upload,
SHA256 `cc07c07596921acc9ea494ffe45a43b0939d10df369b8efdb9c65ad0ab1b7863`.
The prefix is a vector-like sequence of 16-bit words beginning E40E/0020.
This is only the instruction-RAM portion, not the complete program. The
instruction set is not identified; decoding these bytes as the main
CPU's C-SKY ABIv1 instructions is not meaningful. No general coprocessor instruction interpreter has been implemented; the
opt-in measured bootstrap transition is described below.

V3.93 uploads its own 4096-byte image from guest address 0x904856E0, through
the loop at 0x901AA626. Its source pointer comes from the structure referenced
at literal 0x901AA68C, offset +4. The exact upload is preserved as
`analysis/v393-av-microcode.bin`, SHA256
`9a1631693db0a17addf5faf9ccb9204cfa488f20396238e4d68fd065951a8441`.
It shares the initial vector-like prefix but differs from the backup's image.
The wait is at 0x901AA664. Audio-MCU/DSP strings occur in the kernel, but the
instruction-set identity has not been established from them.

The read-only resident monitor returned **94D94ACE** from +160, with CRC
and transaction ID verification, in
`traces/resident-av-command-20261114T0208.json`. This is a post-bootstrap
sample before native coprocessor initialization, not the response to the
guest's later upload/start sequence. It is retained as evidence and has not
been substituted for live command behavior. No physical AV writes were sent.

Next work is to identify the uploaded instruction set and implement its
startup/mailbox effects, or obtain a bounded physical trace of that startup.
Returning zero merely to release the main CPU would omit those effects.
The resident monitor remains available for specific firmware-requested reads
without another power cycle. It has no write or execute command.

## Complete DRAM program layout

Both kernels prepare **0x3000 bytes / 6144 halfwords**, in addition to writing
the first 2048 halfwords through the instruction port. Each group `[a,b,c,d]`
is stored in DRAM as the two little-endian words `[(c<<16)|d,(a<<16)|b]`.
The complete packed image matches guest DRAM byte for byte in both saved
states. The size comes from the backup structure at 0x905D5E60 +0 and the
V3.93 structure referenced at literal 0x901AA68C +8. Both code pointers are
at structure offset +4; V3.93's offset +0 is a selection value, not a length.

| Image | Full source SHA256 | Packed physical DRAM address |
| --- | --- | --- |
| Receiver backup | `a41ec193a6665de571332ab23e994fbe9190f0afc54a07dcee62351d80406717` | `0x1263F000` |
| V3.93 | `f85906d314851fce79231e22b21d573e4e6400187dc9108662a0c7f4dc71cf5e` | `0x12633000` |

The artifacts are `analysis/{receiver,v393}-av-full-program.bin` and their
`-packed-program.bin` counterparts. `analysis/av-full-program-layout-20261114.json`
records the native configuration words. In particular, the meaning of +58
has not been established: the native writes are 0x1E000 and 0x3000, matching
different buffer layouts. The earlier description of this word as an interrupt
mask was not supported and should not be relied on.

`starsat_emu/av_mcu.py` now tracks the indexed upload without treating the
write port as an ordinary register. It retains all 2048 halfwords, validates
upload bounds, and stops explicitly when execution is required. The optional
profile `analysis/gx6605s-av-upload-profile.json` enables this tracking. No
instruction-set or mailbox-completion behavior is claimed.

## Prepared physical startup trace

`tools/serial_av_startup_probe.py` contains a fixed experiment for both pinned,
unmodified native programs. It stops the AV core, clears only reserved AV DRAM,
recreates the measured native packing/configuration, uploads the instruction
RAM, starts the core and polls +160 with a finite limit. It stops the core and
captures 0x90000 bytes of AV RAM for each run. The second image is a warm
stop/reload and is explicitly labeled as such. No SPI/OTP/GCTL commands are
issued by the custom program. CPU IRQ is disabled throughout the experiment.

The reply has CRC32 over its metadata and both RAM captures. Afterward the
program announces and enters a read-only resident monitor. Its extra read-only
RAM window is exactly 0x12633000..0x126C3000; it has no target-write or execute
command. A single physical reload is needed to replace the old reader, after
which subsequent register and captured-state queries need no power cycle.

`traces/av-startup-probe-offline-20261114.json` executes the pinned stage-1
continuation and fixed experiment against a synthetic outcome fixture. It
checks both full native uploads, their packed DRAM bytes, command bounds,
capture CRC and the resident-reader handoff. Unit tests also cover timeout
frames, corrupt/truncated output and read-only queries after handoff. These
checks do not predict whether either physical AV startup will succeed.

## Physical startup result and bounded behavioral model

The CRC-verified physical capture is
`traces/serial-av-startup-20261114T082509334521Z.json`. The receiver program
completed its initial mailbox wait after three polls, then was stopped for
capture. All 589,824 AV DRAM bytes exactly match the prepared emulator state;
`analysis/av-startup-ram-diff-20261114.json` records zero changed bytes.
The warm V3.93 attempt was skipped for nonzero initial +18 (0x8B). Neither
its repeated RAM snapshot nor the shared vector prefix validates V3.93 startup.

The following resident reads confirm stopped control=0, +18=8B and zero in
+20/+160/+164/+168/+170/+174. The register trace is
`traces/serial-register-monitor-20261114T083012718413Z.json`. Word 0x8B is
near a probable mailbox-wait loop in the program, but calling +18 the MCU PC
remains an inference, not a decoded ISA fact.

`analysis/gx6605s-receiver-av-bootstrap-profile.json` explicitly opts into
`receiver-trace-20261114`: a behavioral model of this single startup, bounded
by exact IRAM SHA256, full AV DRAM SHA256 and exact host configuration.
Completion is immediate rather than physically timed. It leaves guest DRAM
and interrupt state unchanged and rejects later commands. The stopped +18
observation is also exposed as representative running idle status, an explicit
approximation. The upload-only profile and packaged app do not enable it.

The unchanged main kernel then reaches 0x0420105C=10 at PC 0x90160DCA. This
is the next unsupported channel command boundary, not a successful menu boot.
Version-2 capture can place V3.93 first and samples +18 before each stop;
version-1 packet bytes and validation remain unchanged.
