"""The MADE 4x4 block image codec.

Every picture and animation frame is stored as three parallel streams:

* the **cmd** stream -- two bits per 4x4 block, packed little-endian into
  16-bit words, ``line_size`` bytes per row of blocks;
* the **pixel** stream -- palette indices;
* the **mask** stream -- the per-pixel selectors.

The two command bits mean:

===  ==========================================================
  0  solid block: 1 pixel byte
  1  two colours: 2 pixel bytes + a 16-bit mask (1 bit/pixel)
  2  four colours: 4 pixel bytes + a 32-bit mask (2 bits/pixel)
  3  in a delta (animation) frame: leave the block untouched.
     Otherwise: 16 raw pixel bytes read from the *mask* stream.
===  ==========================================================

Both the pixel and mask streams can be in nibble mode (flag bit 1), which the
Return to Zork data never uses but which is implemented here for completeness.

Blocks are laid out left to right, then top to bottom, in rows of four scan
lines.  Images are padded up to whole blocks in both directions; the padding is
decoded but never copied out.
"""

import struct

__all__ = ["decompress_image", "compress_image", "line_size_for", "block_stats"]


def line_size_for(width):
    """Bytes of command stream per row of blocks (2 bits per block)."""
    blocks = (width + 3) // 4
    return (blocks + 3) // 4


class _ValueReader(object):
    __slots__ = ("buf", "pos", "nibble", "switch")

    def __init__(self, buf, pos, nibble):
        self.buf = buf
        self.pos = pos
        self.nibble = nibble
        self.switch = False

    def pixel(self):
        if self.nibble:
            if self.switch:
                v = (self.buf[self.pos] >> 4) & 0x0F
                self.pos += 1
            else:
                v = self.buf[self.pos] & 0x0F
            self.switch = not self.switch
            return v
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def u16(self):
        v = self.buf[self.pos] | (self.buf[self.pos + 1] << 8)
        self.pos += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def reset_nibble(self):
        self.switch = False


def decompress_image(source, width, height, cmd_offs, pixel_offs, mask_offs,
                     line_size, cmd_flags=0, pixel_flags=0, mask_flags=0,
                     delta_frame=False, base=None, stats=None):
    """Decode one image.  Returns a ``bytearray`` of ``width * height`` indices.

    ``base`` supplies the previous frame for delta animations; when omitted a
    transparent (zero-filled) canvas is used.
    """
    if cmd_flags:
        raise ValueError("unsupported cmd flags 0x%02X" % cmd_flags)

    out = bytearray(base) if base is not None else bytearray(width * height)
    if base is not None and len(out) != width * height:
        raise ValueError("base frame size mismatch")

    mask = _ValueReader(source, mask_offs, bool(mask_flags & 2))
    pixel = _ValueReader(source, pixel_offs, bool(pixel_flags & 2))
    cmd_pos = cmd_offs

    last_ofs = (((line_size + 1) >> 1) << 1) - 2
    last_count = ((width + 3) >> 2) & 7
    if last_count == 0:
        last_count = 8

    line = bytearray(320 * 4)
    y0 = 0
    while y0 < height:
        for i in range(len(line)):
            line[i] = 0
        row_cmds = bytes(source[cmd_pos:cmd_pos + line_size]) + b"\x00\x00"
        cmd_pos += line_size

        dest = 0
        for ofs in range(0, line_size, 2):
            bits = row_cmds[ofs] | (row_cmds[ofs + 1] << 8)
            n = last_count if ofs == last_ofs else 8
            for _ in range(n):
                cmd = bits & 3
                bits >>= 2
                if stats is not None:
                    stats[cmd] = stats.get(cmd, 0) + 1

                if cmd == 0:
                    p = pixel.pixel()
                    blk = bytes((p, p, p, p))
                    line[dest:dest + 4] = blk
                    line[dest + 320:dest + 324] = blk
                    line[dest + 640:dest + 644] = blk
                    line[dest + 960:dest + 964] = blk
                elif cmd == 1:
                    p0 = pixel.pixel()
                    p1 = pixel.pixel()
                    m = mask.u16()
                    pal = (p0, p1)
                    blk = bytes(pal[(m >> i) & 1] for i in range(16))
                    line[dest:dest + 4] = blk[0:4]
                    line[dest + 320:dest + 324] = blk[4:8]
                    line[dest + 640:dest + 644] = blk[8:12]
                    line[dest + 960:dest + 964] = blk[12:16]
                elif cmd == 2:
                    pal = (pixel.pixel(), pixel.pixel(), pixel.pixel(), pixel.pixel())
                    m = mask.u32()
                    blk = bytes(pal[(m >> (2 * i)) & 3] for i in range(16))
                    line[dest:dest + 4] = blk[0:4]
                    line[dest + 320:dest + 324] = blk[4:8]
                    line[dest + 640:dest + 644] = blk[8:12]
                    line[dest + 960:dest + 964] = blk[12:16]
                elif not delta_frame:
                    mask.reset_nibble()
                    blk = bytes(mask.pixel() for _ in range(16))
                    line[dest:dest + 4] = blk[0:4]
                    line[dest + 320:dest + 324] = blk[4:8]
                    line[dest + 640:dest + 644] = blk[8:12]
                    line[dest + 960:dest + 964] = blk[12:16]
                dest += 4

        for y in range(4):
            if y0 + y >= height:
                break
            src = line[y * 320:y * 320 + width]
            row = (y0 + y) * width
            if delta_frame:
                for x in range(width):
                    v = src[x]
                    if v:
                        out[row + x] = v
            else:
                out[row:row + width] = src
        y0 += 4

    return out


def block_stats(source, width, height, cmd_offs, line_size):
    """Count how often each command appears (cheap; ignores the data streams)."""
    stats = {0: 0, 1: 0, 2: 0, 3: 0}
    last_ofs = (((line_size + 1) >> 1) << 1) - 2
    last_count = ((width + 3) >> 2) & 7 or 8
    pos = cmd_offs
    for _ in range((height + 3) // 4):
        row = bytes(source[pos:pos + line_size]) + b"\x00\x00"
        pos += line_size
        for ofs in range(0, line_size, 2):
            bits = row[ofs] | (row[ofs + 1] << 8)
            n = last_count if ofs == last_ofs else 8
            for _ in range(n):
                stats[bits & 3] += 1
                bits >>= 2
    return stats


# --------------------------------------------------------------------------
# encoder
# --------------------------------------------------------------------------

def _nearest(palette, target, choices):
    tr, tg, tb = palette[target * 3], palette[target * 3 + 1], palette[target * 3 + 2]
    best = choices[0]
    bestd = 1 << 30
    for c in choices:
        dr = palette[c * 3] - tr
        dg = palette[c * 3 + 1] - tg
        db = palette[c * 3 + 2] - tb
        d = dr * dr + dg * dg + db * db
        if d < bestd:
            bestd = d
            best = c
    return best


def _reduce_to_four(block, palette):
    """Pick four palette indices that best represent a >4-colour block."""
    counts = {}
    for v in block:
        counts[v] = counts.get(v, 0) + 1
    candidates = sorted(counts, key=lambda c: (-counts[c], c))
    chosen = candidates[:4]

    def error(sel):
        total = 0
        for v, n in counts.items():
            c = _nearest(palette, v, sel)
            dr = palette[c * 3] - palette[v * 3]
            dg = palette[c * 3 + 1] - palette[v * 3 + 1]
            db = palette[c * 3 + 2] - palette[v * 3 + 2]
            total += n * (dr * dr + dg * dg + db * db)
        return total

    best_err = error(chosen)
    for slot in range(len(chosen)):
        for cand in candidates:
            if cand in chosen:
                continue
            trial = list(chosen)
            trial[slot] = cand
            e = error(trial)
            if e < best_err:
                best_err = e
                chosen = trial
    return chosen


SCRATCH_STRIDE = 320


def _effective(block_index, row, blocks):
    """Is this block's write to the scratch line buffer the one that survives?

    The decoder assembles each band of four scan lines in a buffer with a fixed
    stride of 320, so for a picture wider than 320 pixels a later block's row 0
    lands on top of an earlier block's row 1.  Whatever gets overwritten is a
    don't-care as far as the encoder is concerned.
    """
    step = SCRATCH_STRIDE // 4
    for earlier in range(row):
        if block_index + step * (row - earlier) < blocks:
            return False
    return True


def compress_image(indices, width, height, palette=None, delta_frame=False,
                   allow_raw=False, transparent=0):
    """Encode an image into (cmd, pixel, mask) streams.

    ``indices``    -- width*height palette indices
    ``palette``    -- 768-byte RGB table, used when a block needs colour
                      reduction.  A greyscale ramp is assumed if omitted.
    ``delta_frame``-- emit command 3 for blocks that are entirely
                      ``transparent``; the engine then leaves them untouched.
    ``allow_raw``  -- emit command 3 with 16 literal bytes for blocks with more
                      than four colours instead of reducing them.  Lossless,
                      but only valid for non-delta images.

    Returns ``(cmd, pixel, mask)`` as bytes objects.
    """
    if palette is None:
        palette = bytes(bytearray([i for v in range(256) for i in (v, v, v)]))

    blocks = (width + 3) // 4
    bands = (height + 3) // 4
    line_size = line_size_for(width)
    scratch_len = 3 * SCRATCH_STRIDE + max(width, SCRATCH_STRIDE) + 4

    cmd = bytearray()
    pixel = bytearray()
    mask = bytearray()

    for band in range(bands):
        values = bytearray(scratch_len)
        care = bytearray(scratch_len)
        for y in range(4):
            line = band * 4 + y
            if line >= height:
                break
            base = line * width
            for x in range(width):
                p = y * SCRATCH_STRIDE + x
                values[p] = indices[base + x]
                care[p] = 1

        row_cmds = []
        for b in range(blocks):
            block = bytearray(16)
            wanted = []
            for r in range(4):
                live = _effective(b, r, blocks)
                for c in range(4):
                    p = 4 * b + SCRATCH_STRIDE * r + c
                    if live and care[p]:
                        block[r * 4 + c] = values[p]
                        wanted.append(r * 4 + c)

            uniq = []
            for i in wanted:
                if block[i] not in uniq:
                    uniq.append(block[i])

            if not uniq:
                # nothing in this block is ever read; emit the cheapest thing
                row_cmds.append(3 if delta_frame else 0)
                if not delta_frame:
                    pixel.append(0)
                continue

            if delta_frame and len(uniq) == 1 and uniq[0] == transparent:
                row_cmds.append(3)
                continue

            if len(uniq) == 1:
                row_cmds.append(0)
                pixel.append(uniq[0])
            elif len(uniq) == 2:
                row_cmds.append(1)
                pixel.append(uniq[0])
                pixel.append(uniq[1])
                m = 0
                for i in wanted:
                    if block[i] == uniq[1]:
                        m |= 1 << i
                mask += struct.pack("<H", m)
            elif len(uniq) <= 4:
                row_cmds.append(2)
                sel = list(uniq) + [uniq[0]] * (4 - len(uniq))
                pixel += bytes(sel)
                m = 0
                for i in wanted:
                    m |= sel.index(block[i]) << (2 * i)
                mask += struct.pack("<I", m)
            elif allow_raw and not delta_frame:
                row_cmds.append(3)
                mask += bytes(block)
            else:
                sel = list(_reduce_to_four(bytes(block[i] for i in wanted), palette))
                sel = sel + [sel[0]] * (4 - len(sel))
                row_cmds.append(2)
                pixel += bytes(sel)
                m = 0
                for i in wanted:
                    m |= sel.index(_nearest(palette, block[i], sel)) << (2 * i)
                mask += struct.pack("<I", m)

        # four 2-bit commands per byte, low bits first
        packed = bytearray(line_size)
        for i, c in enumerate(row_cmds):
            packed[i >> 2] |= (c & 3) << (2 * (i & 3))
        cmd += packed

    return bytes(cmd), bytes(pixel), bytes(mask)


# --------------------------------------------------------------------------
# the PMV movie variant of the codec
# --------------------------------------------------------------------------
#
# Movies use the same command/pixel/mask streams, but a frame is drawn straight
# into the previous frame's surface: command 3 means "this block is unchanged",
# which is what makes the format an inter-frame codec.  Blocks walk the whole
# frame left to right and top to bottom rather than being clipped per line.

def decompress_movie_image(source, width, height, cmd_offs, pixel_offs, mask_offs,
                           line_size, prev=None):
    out = bytearray(prev) if prev is not None else bytearray(width * height)
    if len(out) != width * height:
        raise ValueError("previous frame size mismatch")

    cmd_pos = cmd_offs
    pixel_pos = pixel_offs
    mask_pos = mask_offs
    bw = ((width + 3) // 4) * 4
    bx = by = 0

    last_ofs = (((line_size + 1) >> 1) << 1) - 2
    last_count = ((width + 3) >> 2) & 7 or 8

    rows = (height + 3) // 4
    for _ in range(rows):
        row = bytes(source[cmd_pos:cmd_pos + line_size]) + b"\x00\x00"
        cmd_pos += line_size
        for ofs in range(0, line_size, 2):
            bits = row[ofs] | (row[ofs + 1] << 8)
            n = last_count if ofs == last_ofs else 8
            for _i in range(n):
                cmd = bits & 3
                bits >>= 2
                if cmd == 0:
                    p = source[pixel_pos]
                    pixel_pos += 1
                    block = bytes((p,)) * 16
                elif cmd == 1:
                    p0 = source[pixel_pos]
                    p1 = source[pixel_pos + 1]
                    pixel_pos += 2
                    m = source[mask_pos] | (source[mask_pos + 1] << 8)
                    mask_pos += 2
                    pal = (p0, p1)
                    block = bytes(pal[(m >> i) & 1] for i in range(16))
                elif cmd == 2:
                    pal = tuple(source[pixel_pos:pixel_pos + 4])
                    pixel_pos += 4
                    m = struct.unpack_from("<I", source, mask_pos)[0]
                    mask_pos += 4
                    block = bytes(pal[(m >> (2 * i)) & 3] for i in range(16))
                else:
                    block = None

                if block is not None:
                    maxw = min(4, width - bx)
                    maxh = min(4, height - by)
                    for yc in range(maxh):
                        base = (by + yc) * width + bx
                        out[base:base + maxw] = block[yc * 4:yc * 4 + maxw]

                bx += 4
                if bx >= bw:
                    bx = 0
                    by += 4
    return out


def compress_movie_image(indices, width, height, prev=None, palette=None):
    """Encode one movie frame against the previous one.

    Blocks identical to the previous frame become command 3 and cost nothing.
    There is no literal block mode here, so a block with more than four colours
    is reduced to four -- the same limitation the original encoder had.
    """
    if palette is None:
        palette = bytes(bytearray([i for v in range(256) for i in (v, v, v)]))

    bw = (width + 3) // 4
    bh = (height + 3) // 4
    line_size = line_size_for(width)

    cmd = bytearray()
    pixel = bytearray()
    mask = bytearray()

    def fetch(src, bx, by):
        out = []
        for r in range(4):
            y = by * 4 + r
            if y >= height:
                y = height - 1
            for c in range(4):
                x = bx * 4 + c
                if x >= width:
                    x = width - 1
                out.append(src[y * width + x])
        return bytes(out)

    for by in range(bh):
        row_cmds = []
        for bx in range(bw):
            block = fetch(indices, bx, by)
            if prev is not None and fetch(prev, bx, by) == block:
                row_cmds.append(3)
                continue

            uniq = []
            for v in block:
                if v not in uniq:
                    uniq.append(v)
            if len(uniq) == 1:
                row_cmds.append(0)
                pixel.append(uniq[0])
            elif len(uniq) == 2:
                row_cmds.append(1)
                pixel += bytes(uniq)
                m = 0
                for i, v in enumerate(block):
                    if v == uniq[1]:
                        m |= 1 << i
                mask += struct.pack("<H", m)
            else:
                if len(uniq) <= 4:
                    sel = list(uniq) + [uniq[0]] * (4 - len(uniq))
                    lookup = {v: sel.index(v) for v in uniq}
                else:
                    sel = list(_reduce_to_four(block, palette))
                    sel = sel + [sel[0]] * (4 - len(sel))
                    lookup = {}
                row_cmds.append(2)
                pixel += bytes(sel)
                m = 0
                for i, v in enumerate(block):
                    k = lookup.get(v)
                    if k is None:
                        k = sel.index(_nearest(palette, v, sel))
                    m |= k << (2 * i)
                mask += struct.pack("<I", m)

        packed = bytearray(line_size)
        for i, c in enumerate(row_cmds):
            packed[i >> 2] |= (c & 3) << (2 * (i & 3))
        cmd += packed

    return bytes(cmd), bytes(pixel), bytes(mask)
