# StarSat SR-4040HD VEGA firmware: initial analysis

Source: `SR-4040HD_VEGA_V3.93_14102026.bin`

- Size: 3,277,056 bytes (256 bytes plus 0x320000 bytes).
- SHA-256: `10c0962a5f1fef6a5d0313e5e795dd376b9148cc4221696db9c2e99471b4d5b3`
- The source file was read only; no firmware changes or flash operations were performed.

## Confirmed findings

A valid little-endian CramFS begins at file offset `0x296100`. Its declared size is `0x63000` (405,504 bytes). Its stored CRC32 (`32ea55f7`) matches the calculated CRC32 with the checksum field cleared. Every file block decompressed successfully with zlib, and all extracted lengths match their inodes.

The filesystem was saved as `filesystem.cramfs`; its files are under `cramfs/`. `filesystem-manifest.json` records file sizes and SHA-256 hashes. Empty directories were preserved. Nine regular files were extracted.

The configuration `cramfs/etc/gx.cfg` explicitly contains `MODEID=SR-4040HD VEGA`, `CUSTOMERID=3`, `HARDWAREID=5`, and `RCUTYPE=41`. Its `SWVERSION=100` field does not independently establish the V3.93 release in the filename. Feature flags are configuration data, not proof that changing them enables corresponding software or hardware.

Theme resources include `bmp.bin`, `logoinstall.bin`, `logoradio.bin`, and `logostart.bin`. The latter three contain MPEG video sequences; `.bin` does not mean they are raw bitmap images. `home/audio.mp3` is also present.

Readable bootloader-area strings include `chipid=0x6605`, `GX6605S`, `GX3200`, `U-Boot`, `ecos.bin.gz`, serial download/dump command names, and `console=ttyS0,115200`. These suggest a GX6605S-family target and a boot console configured for 115200 baud; actual hardware, accessible commands, electrical interface, and active console settings have not been verified. Generic Linux-related strings alone do not establish the running operating system.

## Unresolved structure and modification limits

The first 256 bytes are not yet decoded. Code/data begins at offset `0x100`, where `aa55aa55` appears. This layout is consistent with a wrapper preceding an aligned flash payload, but that interpretation remains provisional. The system `file` utility labels the whole source as an OpenPGP secret key; the embedded executable-looking data and validated firmware filesystem make this an unreliable classification of the complete file.

A large region beginning around `0x18100` has entropy near 8 bits per byte. No validated gzip, ELF, SquashFS, or U-Boot uImage header was found in the raw image. Subsequent boot emulation established C-SKY ABIv1 boot code and identified this region as the encoded KERNEL partition. The loader processes it through custom decoding passes before attempting to locate `ecos.bin.gz`; application startup is still blocked by unresolved VPU register input. See `docs/boot-status.md` for the current evidence and exact boundary.

Partition metadata was subsequently located at file `0x17F00`, with BOOT, TABLE, KERNEL, ROOT and DATA entries. BOOT and KERNEL CRC32 metadata match the original image. Outer update checksum/signature rules and receiver acceptance checks remain unknown. Extracting and changing an asset does not yet yield a verified flashable update.
