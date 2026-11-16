#!/usr/bin/env python3
"""Build a clean dark-slate/cyan UX/UI duplicate firmware image from the recovery snapshot."""
from pathlib import Path
import struct
import zlib


def rgba_to_bgra(r: int, g: int, b: int, a: int = 255) -> bytes:
    return bytes((b, g, r, a))


def make_canvas(w: int, h: int, color=(0, 0, 0, 0)) -> bytearray:
    px = rgba_to_bgra(*color)
    return bytearray(px * (w * h))


def blend_px(buf: bytearray, w: int, h: int, x: int, y: int, r: int, g: int, b: int, a: int):
    if not (0 <= x < w and 0 <= y < h) or a <= 0:
        return
    idx = (y * w + x) * 4
    if a == 255:
        buf[idx:idx + 4] = bytes((b, g, r, 255))
        return
    db, dg, dr, da = buf[idx], buf[idx + 1], buf[idx + 2], buf[idx + 3]
    inv = 255 - a
    oa = a + (da * inv) // 255
    if oa == 0:
        return
    buf[idx] = (b * a + db * da * inv // 255) // oa
    buf[idx + 1] = (g * a + dg * da * inv // 255) // oa
    buf[idx + 2] = (r * a + dr * da * inv // 255) // oa
    buf[idx + 3] = oa


def draw_rounded_rect(buf: bytearray, w: int, h: int, x0: int, y0: int, x1: int, y1: int, radius: int,
                      fill, border=None, border_w: int = 1):
    r2 = radius * radius
    ri = max(0, radius - border_w)
    ri2 = ri * ri
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            dx = 0
            dy = 0
            if x < x0 + radius:
                dx = (x0 + radius) - x
            elif x >= x1 - radius:
                dx = x - (x1 - radius - 1)
            if y < y0 + radius:
                dy = (y0 + radius) - y
            elif y >= y1 - radius:
                dy = y - (y1 - radius - 1)
            d2 = dx * dx + dy * dy
            if d2 <= r2:
                is_border = border is not None and (
                    x < x0 + border_w or x >= x1 - border_w or
                    y < y0 + border_w or y >= y1 - border_w or
                    (dx > 0 and dy > 0 and d2 > ri2)
                )
                col = border if is_border else fill
                blend_px(buf, w, h, x, y, *col)


def draw_fill_rect(buf: bytearray, w: int, h: int, x0: int, y0: int, x1: int, y1: int, col):
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            blend_px(buf, w, h, x, y, *col)


def draw_circle(buf: bytearray, w: int, h: int, cx: int, cy: int, radius: int, col):
    r2 = radius * radius
    for y in range(max(0, cy - radius), min(h, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(w, cx + radius + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                blend_px(buf, w, h, x, y, *col)


def build_clean_asset(name: str, w: int, h: int, orig_raw: bytes):
    SLATE_950 = (11, 17, 30, 245)
    SLATE_900 = (15, 23, 42, 245)
    SLATE_800 = (30, 41, 59, 248)
    SLATE_700 = (51, 65, 85, 255)
    CYAN = (56, 189, 248, 255)
    CYAN_SOFT = (14, 116, 144, 220)
    AMBER = (245, 158, 11, 255)

    if name == "menubar.png":
        buf = make_canvas(w, h)
        draw_rounded_rect(buf, w, h, 8, 4, w - 8, h - 4, 12, SLATE_900, SLATE_700, 1)
        draw_fill_rect(buf, w, h, w // 2 - 48, 4, w // 2 + 48, 6, CYAN)
        return bytes(buf)

    if name == "menufontbar.png":
        buf = make_canvas(w, h)
        draw_rounded_rect(buf, w, h, 2, 2, w - 2, h - 10, 10, SLATE_900, CYAN, 2)
        # Clean downward pointer triangle centered at bottom
        cx = w // 2
        for dy in range(8):
            span = 8 - dy
            draw_fill_rect(buf, w, h, cx - span, h - 10 + dy, cx + span + 1, h - 9 + dy, SLATE_900)
            blend_px(buf, w, h, cx - span, h - 10 + dy, *CYAN)
            blend_px(buf, w, h, cx + span, h - 10 + dy, *CYAN)
        return bytes(buf)

    if name in ("submenu.png", "submenu1.png"):
        buf = make_canvas(w, h)
        draw_rounded_rect(buf, w, h, 2, 2, w - 2, h - 2, 12, SLATE_900, SLATE_700, 1)
        draw_rounded_rect(buf, w, h, 3, 3, w - 3, 42, 10, SLATE_800)
        draw_fill_rect(buf, w, h, 3, 30, w - 3, 42, SLATE_800)
        draw_fill_rect(buf, w, h, 14, 41, w - 14, 43, CYAN)
        return bytes(buf)

    if name in ("channelinfobar.png", "GVODinfobar.png"):
        buf = make_canvas(w, h)
        draw_rounded_rect(buf, w, h, 4, 4, w - 4, h - 4, 14, SLATE_900, SLATE_700, 1)
        draw_fill_rect(buf, w, h, 4, 18, 8, h - 18, CYAN)
        draw_fill_rect(buf, w, h, 16, 40, w - 16, 42, SLATE_700)
        return bytes(buf)

    if name == "singalweakbar.png":
        buf = make_canvas(w, h)
        draw_rounded_rect(buf, w, h, 2, 2, w - 2, h - 2, 12, SLATE_950, SLATE_700, 1)
        draw_fill_rect(buf, w, h, 2, 12, 6, h - 12, AMBER)
        return bytes(buf)

    if name == "warning.png":
        buf = make_canvas(w, h)
        cx, cy = w // 2, h // 2
        draw_circle(buf, w, h, cx, cy, min(w, h) // 2 - 6, AMBER)
        draw_rounded_rect(buf, w, h, cx - 4, cy - 18, cx + 4, cy + 5, 3, SLATE_950)
        draw_circle(buf, w, h, cx, cy + 14, 4, SLATE_950)
        return bytes(buf)

    if name in ("editlistbar2.png", "editlightbar.png", "Gvod_select.png"):
        buf = make_canvas(w, h)
        draw_rounded_rect(buf, w, h, 0, 1, w, h - 1, min(6, h // 4), CYAN_SOFT, CYAN, 1)
        return bytes(buf)

    if name.startswith("menubigicon") or name.startswith("menuicon"):
        # Frame the existing icon inside a clean dark-glass tile (active=cyan border, inactive=slate)
        is_active = name.startswith("menubigicon")
        buf = make_canvas(w, h)
        pad_x, pad_y = 4, 10
        draw_rounded_rect(
            buf, w, h, pad_x, pad_y, w - pad_x, h - pad_y, 14,
            SLATE_800 if is_active else SLATE_900,
            CYAN if is_active else SLATE_700,
            2 if is_active else 1,
        )
        # Composite original icon pixels over the dark-glass tile
        for y in range(h):
            for x in range(w):
                idx = (y * w + x) * 4
                b, g, r, a = orig_raw[idx], orig_raw[idx + 1], orig_raw[idx + 2], orig_raw[idx + 3]
                if a > 12:
                    blend_px(buf, w, h, x, y, r, g, b, a)
        return bytes(buf)

    if name.startswith("whiteframe"):
        # Recolor legacy light/white frame pieces to dark slate
        out = bytearray(orig_raw)
        for i in range(0, len(out), 4):
            a = out[i + 3]
            if a > 0:
                lum = (out[i] + out[i + 1] + out[i + 2]) // 3
                if lum > 150:
                    out[i], out[i + 1], out[i + 2] = 42, 23, 15  # #0F172A in BGRA
                else:
                    out[i], out[i + 1], out[i + 2] = 85, 65, 51  # #334155 border
        return bytes(out)

    return None


def parse_cramfs(data: bytes, base: int):
    def read_inode(off):
        w0, w1, w2 = struct.unpack_from("<3I", data, base + off)
        return (w0 & 0xffff), (w1 & 0xffffff), ((w2 & 0x3f) * 4), ((w2 >> 6) * 4)

    def extract_file(isize, ioff):
        if isize == 0:
            return b""
        nblocks = (isize + 4095) // 4096
        ptrs = struct.unpack_from(f"<{nblocks}I", data, base + ioff)
        buf = bytearray()
        prev = ioff + nblocks * 4
        for p in ptrs:
            buf.extend(zlib.decompress(data[base + prev:base + p]))
            prev = p
        return bytes(buf[:isize])

    def walk(off, size):
        tree = {}
        pos = off
        while pos < off + size:
            mode, isize, namelen, ioff = read_inode(pos)
            name = data[base + pos + 12:base + pos + 12 + namelen].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            ftype = (mode >> 12) & 0xf
            if ftype == 4:
                tree[name] = (mode, walk(ioff, isize) if isize > 0 else {})
            else:
                tree[name] = (mode, extract_file(isize, ioff))
            pos += 12 + namelen
        return tree

    rmode, rsize, _, roff = read_inode(64)
    return rmode, walk(roff, rsize)


def retheme_bmp_bin(bmp: bytes) -> bytes:
    pos = 8
    entries = []
    while pos < len(bmp) and bmp[pos] == 0x02 and bmp[pos + 1] == 0x02:
        rec_len = struct.unpack_from(">H", bmp, pos + 2)[0]
        rec = bmp[pos + 4:pos + 4 + rec_len]
        idx = struct.unpack_from(">H", rec, 0)[0]
        nlen = rec[4]
        name = rec[5:5 + nlen].decode("ascii", errors="replace")
        sub = rec[5 + nlen:]
        w, h, doff = struct.unpack_from(">HHI", sub, 2)
        csize = int.from_bytes(sub[10:13], "big")
        rsize = int.from_bytes(sub[13:16], "big")
        entries.append((idx, name, w, h, doff, csize, rsize))
        pos += 4 + rec_len

    data_base = pos + 4
    dir_out = bytearray(bmp[:8])
    pixel_out = bytearray(bmp[pos:data_base])
    cur_off = 0
    replaced = 0

    for idx, name, w, h, doff, csize, rsize in entries:
        comp = bmp[data_base + doff:data_base + doff + csize]
        orig_raw = zlib.decompress(comp)
        new_raw = build_clean_asset(name, w, h, orig_raw)
        if new_raw is not None:
            comp = zlib.compress(new_raw, 9)
            rsize = len(new_raw)
            replaced += 1
        csize = len(comp)

        name_b = name.encode("ascii")
        sub = b"\x02\x0e" + struct.pack(">HHI", w, h, cur_off) + csize.to_bytes(3, "big") + rsize.to_bytes(3, "big")
        rec = struct.pack(">H", idx) + b"\x02\x01" + bytes([len(name_b)]) + name_b + sub
        dir_out.extend(b"\x02\x02" + struct.pack(">H", len(rec)) + rec)
        pixel_out.extend(comp)
        cur_off += csize

    print(f"Rethemed {replaced}/{len(entries)} UI assets in theme/bmp.bin")
    return bytes(dir_out + pixel_out)


def build_cramfs(rmode: int, tree: dict, max_size: int) -> bytes:
    # Build CramFS superblock (64B) + inodes + 4KiB zlib blocks
    nodes = []

    def flatten(d: dict):
        items = []
        for name in sorted(d.keys()):
            mode, val = d[name]
            node = {"name": name.encode("utf-8"), "mode": mode, "val": val, "ino_pos": 0, "data_off": 0, "size": 0}
            if isinstance(val, dict):
                node["children"] = flatten(val)
            items.append(node)
        return items

    root_children = flatten(tree)

    # First pass: compute inode layout starting at offset 64 + 12 (root inode at 64)
    cur = 76
    def assign_dir_offsets(children):
        nonlocal cur
        if not children:
            return 0, 0
        start = cur
        for ch in children:
            ch["ino_pos"] = cur
            nlen_pad = ((len(ch["name"]) + 3) // 4) * 4
            cur += 12 + nlen_pad
        size = cur - start
        for ch in children:
            if "children" in ch:
                c_off, c_size = assign_dir_offsets(ch["children"])
                ch["data_off"] = c_off
                ch["size"] = c_size
        return start, size

    root_off, root_size = assign_dir_offsets(root_children)
    data_start = (cur + 3) & ~3
    image = bytearray(data_start)

    # Write file data blocks
    def write_files(children):
        for ch in children:
            if "children" in ch:
                write_files(ch["children"])
            else:
                raw = ch["val"]
                ch["size"] = len(raw)
                if len(raw) == 0:
                    ch["data_off"] = 0
                else:
                    while len(image) % 4 != 0:
                        image.append(0)
                    ch["data_off"] = len(image)
                    nblocks = (len(raw) + 4095) // 4096
                    ptrs_pos = len(image)
                    image.extend(bytes(nblocks * 4))
                    ptrs = []
                    for i in range(nblocks):
                        chunk = raw[i * 4096:(i + 1) * 4096]
                        image.extend(zlib.compress(chunk, 9))
                        ptrs.append(len(image))
                    struct.pack_into(f"<{nblocks}I", image, ptrs_pos, *ptrs)

    write_files(root_children)
    while len(image) % 4096 != 0:
        image.append(0)
    fs_size = len(image)
    if fs_size > max_size:
        raise ValueError(f"CramFS size {fs_size} exceeds slot {max_size}")

    # Write inodes
    def pack_inode(mode, size, namelen_words, data_off):
        w0 = mode & 0xffff
        w1 = size & 0xffffff
        w2 = (namelen_words & 0x3f) | ((data_off >> 2) << 6)
        return struct.pack("<3I", w0, w1, w2)

    struct.pack_into("<12s", image, 64, pack_inode(rmode, root_size, 0, root_off))

    def write_inodes(children):
        for ch in children:
            nlen_pad = ((len(ch["name"]) + 3) // 4) * 4
            padded_name = ch["name"].ljust(nlen_pad, b"\x00")
            ino = pack_inode(ch["mode"], ch["size"], nlen_pad // 4, ch["data_off"])
            image[ch["ino_pos"]:ch["ino_pos"] + 12 + nlen_pad] = ino + padded_name
            if "children" in ch:
                write_inodes(ch["children"])

    write_inodes(root_children)

    # Write superblock (64 bytes) and compute CRC32
    struct.pack_into("<4I16s4I", image, 0,
                     0x28cd3d45, fs_size, 0, 0, b"Compressed ROMFS",
                     0, 0, 0, 0)
    crc = zlib.crc32(image) & 0xffffffff
    struct.pack_into("<I", image, 32, crc)
    print(f"Built CramFS: size={fs_size} (max {max_size}), crc32={crc:08x}")
    return bytes(image)


def main():
    root = Path(__file__).resolve().parent.parent
    src = root / "backups/SR-4040HD_VEGA_recovery_flash_4MB.bin"
    dst = root / "custom_firmware/SR-4040HD_VEGA_clean_ui.flash.bin"
    dst.parent.mkdir(exist_ok=True)

    flash = bytearray(src.read_bytes())
    cramfs_off = 0x270000
    cramfs_slot = 0x70000  # 0x270000..0x2e0000

    rmode, tree = parse_cramfs(flash, cramfs_off)
    mode_bmp, orig_bmp = tree["theme"][1]["bmp.bin"]
    tree["theme"][1]["bmp.bin"] = (mode_bmp, retheme_bmp_bin(orig_bmp))

    new_cramfs = build_cramfs(rmode, tree, cramfs_slot)
    flash[cramfs_off:cramfs_off + cramfs_slot] = new_cramfs + b"\xff" * (cramfs_slot - len(new_cramfs))
    dst.write_bytes(flash)
    print(f"Wrote custom firmware: {dst} ({len(flash)} bytes)")


if __name__ == "__main__":
    main()
