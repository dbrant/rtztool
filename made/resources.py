"""Codecs for the individual MADE resource types.

Every function here works on the *payload* of a resource, i.e. what is left
after :mod:`made.prj` strips the 62-byte DATA header.
"""

import struct

from . import imagecodec

__all__ = ["Picture", "Animation", "AnimFrame", "Sound", "Menu", "Font",
           "decode_picture", "encode_picture", "decode_anim", "encode_anim",
           "decode_sound", "encode_sound", "decode_menu", "encode_menu",
           "decode_font", "encode_font"]

PIC_HEADER_SIZE = 18


# --------------------------------------------------------------------------
# FLEX -- still pictures
# --------------------------------------------------------------------------

class Picture(object):
    def __init__(self, width, height, indices, palette=None):
        self.width = width
        self.height = height
        self.indices = indices
        self.palette = palette      # 768 bytes, or None

    def __repr__(self):
        return "<Picture %dx%d %s>" % (
            self.width, self.height, "paletted" if self.palette else "no palette")


def decode_picture(payload, stats=None):
    if len(payload) < PIC_HEADER_SIZE:
        raise ValueError("picture payload too short (%d bytes)" % len(payload))
    has_palette = payload[0] != 0
    cmd_flags, pixel_flags, mask_flags = payload[1], payload[2], payload[3]
    cmd_offs, pixel_offs, mask_offs, line_size, _rows, width, height = \
        struct.unpack_from("<7H", payload, 4)

    palette = None
    if has_palette:
        n = (cmd_offs - PIC_HEADER_SIZE) // 3
        palette = payload[PIC_HEADER_SIZE:PIC_HEADER_SIZE + n * 3]
        if n < 256:
            palette = palette + b"\x00" * ((256 - n) * 3)

    indices = imagecodec.decompress_image(
        payload, width, height, cmd_offs, pixel_offs, mask_offs, line_size,
        cmd_flags, pixel_flags, mask_flags, stats=stats)
    return Picture(width, height, indices, palette)


def encode_picture(pic, allow_raw=False):
    cmd, pixel, mask = imagecodec.compress_image(
        pic.indices, pic.width, pic.height, pic.palette, allow_raw=allow_raw)

    pal = pic.palette or b""
    cmd_offs = PIC_HEADER_SIZE + len(pal)
    pixel_offs = cmd_offs + len(cmd)
    mask_offs = pixel_offs + len(pixel)
    rows = (pic.height + 3) // 4

    out = bytearray()
    out.append(1 if pal else 0)
    out += b"\x00\x00\x00"
    out += struct.pack("<7H", cmd_offs, pixel_offs, mask_offs,
                       imagecodec.line_size_for(pic.width), rows,
                       pic.width, pic.height)
    out += pal
    out += cmd
    out += pixel
    out += mask
    return bytes(out)


# --------------------------------------------------------------------------
# ANIM -- sprite animations
# --------------------------------------------------------------------------

class AnimFrame(object):
    def __init__(self, width, height, indices, unk0=0, unk1=0):
        self.width = width
        self.height = height
        self.indices = indices
        self.unk0 = unk0
        self.unk1 = unk1


class Animation(object):
    def __init__(self, width, height, flags, frames, header_unk=None):
        self.width = width
        self.height = height
        self.flags = flags
        self.frames = frames
        self.header_unk = header_unk or {}

    @property
    def is_delta(self):
        return bool(self.flags & 1)

    def __repr__(self):
        return "<Animation %dx%d flags=%d %d frames>" % (
            self.width, self.height, self.flags, len(self.frames))


def decode_anim(payload, accumulate=True):
    """Decode an animation.

    Delta animations store only the changes from the previous frame.  With
    ``accumulate`` the frames are composited so each one is a complete picture;
    with ``accumulate=False`` the raw per-frame deltas are returned (0 meaning
    "unchanged").
    """
    if payload[0:4] != b"ANIM":
        raise ValueError("not an ANIM payload (%r)" % payload[0:4])
    unk_a = struct.unpack_from("<H", payload, 8)[0]
    flags, width, height = struct.unpack_from("<3H", payload, 10)
    unk_b = struct.unpack_from("<I", payload, 16)[0]
    frame_count = struct.unpack_from("<H", payload, 20)[0]
    unk_c, unk_d = struct.unpack_from("<2H", payload, 22)

    delta = bool(flags & 1)
    frames = []
    prev = None
    for i in range(frame_count):
        (frame_offs,) = struct.unpack_from("<I", payload, 26 + i * 4)
        f_unk0, f_unk1 = struct.unpack_from("<2I", payload, frame_offs)
        fw, fh = struct.unpack_from("<2H", payload, frame_offs + 8)
        cmd_offs, pixel_offs, mask_offs = struct.unpack_from("<3I", payload, frame_offs + 12)
        (line_size,) = struct.unpack_from("<H", payload, frame_offs + 24)

        base = None
        if delta and accumulate and prev is not None and len(prev) == fw * fh:
            base = prev
        indices = imagecodec.decompress_image(
            memoryview(payload)[frame_offs:], fw, fh,
            cmd_offs, pixel_offs, mask_offs, line_size,
            delta_frame=delta, base=base)
        if delta and accumulate:
            prev = indices
        frames.append(AnimFrame(fw, fh, indices, f_unk0, f_unk1))

    return Animation(width, height, flags, frames,
                     {"a": unk_a, "b": unk_b, "c": unk_c, "d": unk_d})


def encode_anim(anim, palette=None, allow_raw=False):
    frame_blobs = []
    for frame in anim.frames:
        cmd, pixel, mask = imagecodec.compress_image(
            frame.indices, frame.width, frame.height, palette,
            delta_frame=anim.is_delta, allow_raw=allow_raw and not anim.is_delta)
        cmd_offs = 26
        pixel_offs = cmd_offs + len(cmd)
        mask_offs = pixel_offs + len(pixel)
        total = mask_offs + len(mask)
        # the leading u32 is a chunk size that excludes itself
        hdr = struct.pack("<II2H3IH", total - 4, frame.unk1, frame.width, frame.height,
                          cmd_offs, pixel_offs, mask_offs,
                          imagecodec.line_size_for(frame.width))
        frame_blobs.append(hdr + cmd + pixel + mask)

    table_size = 26 + 4 * len(frame_blobs)
    offsets = []
    pos = table_size
    for blob in frame_blobs:
        offsets.append(pos)
        pos += len(blob)

    body = bytearray()
    body += struct.pack("<I", 0)            # size, patched below
    body += struct.pack("<H", anim.header_unk.get("a", 100))
    body += struct.pack("<3H", anim.flags, anim.width, anim.height)
    body += struct.pack("<I", anim.header_unk.get("b", 0))
    body += struct.pack("<H", len(frame_blobs))
    body += struct.pack("<2H", anim.header_unk.get("c", 0), anim.header_unk.get("d", 0))
    for o in offsets:
        body += struct.pack("<I", o)
    for blob in frame_blobs:
        body += blob

    out = bytearray(b"ANIM")
    out += body
    struct.pack_into("<I", out, 4, len(out) - 8)
    return bytes(out)


# --------------------------------------------------------------------------
# SNDS -- compressed speech and effects
# --------------------------------------------------------------------------

_MODE_VALUES = {2: (2, 8, 0x01, 1), 3: (4, 4, 0x03, 2), 4: (16, 2, 0x0F, 4)}


class Sound(object):
    def __init__(self, samples, chunk_size=128):
        self.samples = samples          # unsigned 8-bit PCM
        self.chunk_size = chunk_size

    def __len__(self):
        return len(self.samples)


def _clip(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class SoundDecoderState(object):
    """Carries the decoder's running state between chunks.

    Movie soundtracks decode one buffer per frame but share state across the
    whole film, so the reconstruction has to continue where it left off.
    """

    def __init__(self, chunk_size=128):
        self.buf = bytearray(b"\x80" * (chunk_size + 4))
        self.prev_sample = 0


def decompress_sound(payload, pos, chunk_size, chunk_count, state=None):
    """Expand ``chunk_count`` chunks starting at ``pos``.  Returns PCM bytes."""
    if state is None:
        state = SoundDecoderState(chunk_size)
    if len(state.buf) < chunk_size + 4:
        state.buf = bytearray(state.buf) + b"\x80" * (chunk_size + 4 - len(state.buf))
    buf = state.buf
    delta_buf = bytearray(chunk_size + 4)
    out = bytearray()
    src = pos

    for _ in range(chunk_count):
        delta_type = payload[src] >> 6
        work = chunk_size
        if delta_type == 1:
            work //= 2
        elif delta_type == 2:
            work //= 4
        type_ = payload[src] & 0x0F
        src += 1
        sample = state.prev_sample

        if type_ == 0:
            for i in range(work):
                buf[i] = 0x80
            sample = 0
        elif type_ == 1:
            pass
        elif type_ in (2, 3, 4):
            byte_count, bit_count, bit_mask, bit_shift = _MODE_VALUES[type_]
            table = [payload[src + i] * 2 - 128 for i in range(byte_count)]
            src += byte_count
            ofs = 0
            while ofs < work:
                val = payload[src]
                src += 1
                for _i in range(bit_count):
                    sample = _clip(sample + table[val & bit_mask], -127, 127)
                    val >>= bit_shift
                    buf[ofs] = sample + 128
                    ofs += 1
        elif type_ == 5:
            buf[0:work] = payload[src:src + work]
            src += work
            sample = buf[work - 1] - 128
        else:
            break

        if delta_type > 0:
            buf[work] = buf[work - 1]
            if delta_type == 1:
                for i in range(0, chunk_size - 1, 2):
                    l = i // 2
                    delta_buf[i] = buf[l]
                    delta_buf[i + 1] = (buf[l] + buf[l + 1]) // 2
            else:
                for i in range(0, chunk_size - 1, 4):
                    l = i // 4
                    delta_buf[i] = buf[l]
                    delta_buf[i + 2] = (buf[l] + buf[l + 1]) // 2
                    delta_buf[i + 1] = (delta_buf[i + 2] + buf[l]) // 2
                    delta_buf[i + 3] = (delta_buf[i + 2] + buf[l + 1]) // 2
            buf[0:chunk_size] = delta_buf[0:chunk_size]

        state.prev_sample = sample
        out += buf[0:chunk_size]

    return bytes(out)


def decode_sound(payload):
    if payload[0:4] != b"SFLX":
        raise ValueError("not an SFLX payload (%r)" % payload[0:4])
    (chunk_count,) = struct.unpack_from("<I", payload, 8)
    (chunk_size,) = struct.unpack_from("<H", payload, 12)
    return Sound(decompress_sound(payload, 14, chunk_size, chunk_count), chunk_size)


def encode_sound_chunks(samples, chunk_size):
    """Pack PCM into uncompressed (type 5) chunks."""
    data = bytearray(samples)
    if len(data) % chunk_size:
        data += b"\x80" * (chunk_size - len(data) % chunk_size)
    body = bytearray()
    for i in range(0, len(data), chunk_size):
        body.append(0x05)
        body += data[i:i + chunk_size]
    return bytes(body), len(data) // chunk_size


def encode_sound(sound):
    """Re-encode PCM as uncompressed (type 5) chunks.

    Bit-exact for the samples, at roughly twice the size of the original
    ADPCM-style encoding.  The engine handles type 5 natively.
    """
    chunk_size = sound.chunk_size or 128
    body, chunk_count = encode_sound_chunks(sound.samples, chunk_size)
    out = bytearray(b"SFLX")
    out += struct.pack("<I", 0)
    out += struct.pack("<I", chunk_count)
    out += struct.pack("<H", chunk_size)
    out += body
    struct.pack_into("<I", out, 4, len(out) - 4)
    return bytes(out)


# --------------------------------------------------------------------------
# MENU -- string tables
# --------------------------------------------------------------------------

class Menu(object):
    def __init__(self, strings):
        self.strings = strings


def decode_menu(payload):
    if payload[0:4] != b"MENU":
        raise ValueError("not a MENU payload (%r)" % payload[0:4])
    (count,) = struct.unpack_from("<H", payload, 4)
    strings = []
    for i in range(count):
        (offs,) = struct.unpack_from("<H", payload, 6 + i * 2)
        end = payload.index(b"\x00", offs)
        strings.append(payload[offs:end].decode("cp850"))
    return Menu(strings)


def encode_menu(menu):
    count = len(menu.strings)
    table_size = 6 + count * 2
    blob = bytearray()
    offsets = []
    for s in menu.strings:
        offsets.append(table_size + len(blob))
        blob += s.encode("cp850") + b"\x00"
    out = bytearray(b"MENU")
    out += struct.pack("<H", count)
    for o in offsets:
        out += struct.pack("<H", o)
    out += blob
    return bytes(out)


# --------------------------------------------------------------------------
# FONT -- 1bpp bitmap fonts, characters 28..255
# --------------------------------------------------------------------------

FONT_FIRST_CHAR = 28
FONT_LAST_CHAR = 255


class Font(object):
    """A 1bpp font.  Glyphs are 8 pixels wide, MSB first, ``height`` rows tall.

    Character codes start at 28; how many codes a font actually stores varies
    (99, 190, 228 or 229 in the shipped archive), so the count is preserved.
    """

    def __init__(self, height, glyphs, char_count=None):
        self.height = height
        self.glyphs = glyphs        # {code: (advance_width, [row_bytes])}
        self.char_count = char_count if char_count is not None else len(glyphs)

    @property
    def last_char(self):
        return FONT_FIRST_CHAR + self.char_count - 1


def decode_font(payload):
    height = payload[0]
    stride = height + 1
    count = (len(payload) - 1) // stride
    glyphs = {}
    for i in range(count):
        base = 1 + i * stride
        glyphs[FONT_FIRST_CHAR + i] = (payload[base],
                                       list(payload[base + 1:base + 1 + height]))
    return Font(height, glyphs, count)


def encode_font(font):
    out = bytearray([font.height])
    for i in range(font.char_count):
        code = FONT_FIRST_CHAR + i
        width, rows = font.glyphs.get(code, (0, [0] * font.height))
        out.append(width & 0xFF)
        rows = list(rows)[:font.height]
        rows += [0] * (font.height - len(rows))
        out += bytes(r & 0xFF for r in rows)
    return bytes(out)
