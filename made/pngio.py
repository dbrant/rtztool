"""Minimal dependency-free PNG reader/writer.

Only what the RTZ toolchain needs:
  * write 8-bit palettized PNGs (with optional tRNS)
  * write 8-bit greyscale PNGs
  * read 8-bit PNGs of colour type 0/2/3/6 (non-interlaced)

Reading returns either palette indices (when the file is palettized and its
palette matches what we asked for) or RGB tuples, and the caller decides how to
map back into the game's 256-colour palette.
"""

import struct
import zlib

__all__ = ["write_palette_png", "write_rgb_png", "read_png", "PngImage"]


def _chunk(tag, data):
    out = struct.pack(">I", len(data)) + tag + data
    return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_palette_png(path, width, height, indices, palette, transparent_index=None):
    """Write an 8-bit palettized PNG.

    indices  -- bytes-like, width*height palette indices
    palette  -- bytes-like, 768 bytes of RGB (256 entries)
    """
    if len(indices) != width * height:
        raise ValueError("indices length %d != %d*%d" % (len(indices), width, height))
    pal = bytes(palette[: 256 * 3])
    pal = pal + b"\x00" * (768 - len(pal))

    raw = bytearray()
    mv = memoryview(bytes(indices))
    for y in range(height):
        raw.append(0)  # filter: none
        raw += mv[y * width : (y + 1) * width]

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
    png += _chunk(b"PLTE", pal)
    if transparent_index is not None:
        trns = bytearray(b"\xff" * (transparent_index + 1))
        trns[transparent_index] = 0
        png += _chunk(b"tRNS", bytes(trns))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def write_rgb_png(path, width, height, rgb):
    """Write a truecolour 8-bit PNG from packed RGB bytes."""
    raw = bytearray()
    mv = memoryview(bytes(rgb))
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += mv[y * stride : (y + 1) * stride]
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


class PngImage(object):
    """Decoded PNG: either palettized (indices + palette) or RGBA."""

    def __init__(self, width, height, color_type, pixels, palette=None, trns=None):
        self.width = width
        self.height = height
        self.color_type = color_type
        self.pixels = pixels  # indices (ct 3) or packed RGBA (others)
        self.palette = palette
        self.trns = trns

    @property
    def is_palettized(self):
        return self.color_type == 3

    def to_rgba(self):
        """Return packed RGBA bytes regardless of source colour type."""
        if self.color_type != 3:
            return self.pixels
        pal = self.palette or b""
        trns = self.trns or b""
        out = bytearray(self.width * self.height * 4)
        for i, idx in enumerate(self.pixels):
            out[i * 4 + 0] = pal[idx * 3 + 0] if idx * 3 + 2 < len(pal) else 0
            out[i * 4 + 1] = pal[idx * 3 + 1] if idx * 3 + 2 < len(pal) else 0
            out[i * 4 + 2] = pal[idx * 3 + 2] if idx * 3 + 2 < len(pal) else 0
            out[i * 4 + 3] = trns[idx] if idx < len(trns) else 255
        return bytes(out)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, bpp, stride):
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ft == 0:
            pass
        elif ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError("bad PNG filter type %d" % ft)
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return out


def read_png(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s: not a PNG file" % path)
    pos = 8
    width = height = depth = ct = 0
    palette = None
    trns = None
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, ct, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8:
                raise ValueError("%s: only 8-bit PNGs are supported (got %d)" % (path, depth))
            if interlace:
                raise ValueError("%s: interlaced PNGs are not supported" % path)
        elif tag == b"PLTE":
            palette = bytes(body)
        elif tag == b"tRNS":
            trns = bytes(body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    stride = width * channels
    flat = _unfilter(raw, width, height, channels, stride)

    if ct == 3:
        return PngImage(width, height, 3, bytes(flat), palette, trns)
    # normalise everything else to RGBA
    out = bytearray(width * height * 4)
    n = width * height
    if ct == 0:
        for i in range(n):
            v = flat[i]
            out[i * 4 : i * 4 + 4] = bytes((v, v, v, 255))
    elif ct == 4:
        for i in range(n):
            v = flat[i * 2]
            out[i * 4 : i * 4 + 4] = bytes((v, v, v, flat[i * 2 + 1]))
    elif ct == 2:
        for i in range(n):
            out[i * 4 : i * 4 + 3] = flat[i * 3 : i * 3 + 3]
            out[i * 4 + 3] = 255
    else:
        out = bytearray(flat)
    return PngImage(width, height, ct, bytes(out))
