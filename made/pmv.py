"""Return to Zork's ``.PMV`` cutscenes.

A PMV is a flat chunk stream.  Every chunk is a 4-byte big-endian tag followed
by a 32-bit little-endian size::

    MOVE  the whole file
    MHED  the header
    MFRM  one frame, repeated frame_count times

``MHED``::

     0   2  u16 frame delay in milliseconds
     2   4  unused
     6   2  u16 frame count
     8   4  unused
    12   2  u16 sound sample rate (11127 and 22254 really mean 11025 / 22050)
    14  44  22 u16 values, one per stream slot
    58 768  the starting palette, RGB

``MFRM`` payload::

     0  88  22 u32 stream offsets; slot 0 is the frame number, slot 2 the
            audio sub-chunk, slot 3 the image and slot 4 a palette update.
            Offsets are measured from the start of the MFRM chunk *including*
            its 8-byte header, so subtract 8 to index the payload.
    88  ..  the sub-chunks themselves

Image sub-chunk (the offsets below are relative to its own start, 8 bytes
before the value in the slot table)::

     0   4  u32 payload size
     4   4  unused
     8   2  u16 width
    10   2  u16 height
    12   4  u32 command stream offset
    16   4  u32 pixel stream offset
    20   4  u32 mask stream offset
    24   2  u16 command bytes per row of blocks
    26  ..  the three streams

Audio sub-chunk::

     0   4  unused
     4   2  u16 samples per chunk
     6   2  u16 chunk count
     8   ..  the same compressed audio used by SNDS resources

Palette sub-chunk: a u32 size then runs of ``count, first_index, RGB * (count+1)``,
terminated by ``FF FF``.
"""

import struct

from . import imagecodec
from . import resources

HEADER_SLOTS = 22
FRAME_HEADER_SIZE = HEADER_SLOTS * 4
SLOT_FRAME_NUMBER = 0
SLOT_AUDIO = 2
SLOT_IMAGE = 3
SLOT_PALETTE = 4


class PmvFrame(object):
    def __init__(self, number):
        self.number = number
        self.image = None          # decoded palette indices
        self.width = 0
        self.height = 0
        self.audio = b""           # decoded PCM for this frame
        self.palette_updates = []  # (first_index, rgb bytes)
        self.palette = None        # full palette in effect for this frame


class PmvMovie(object):
    def __init__(self):
        self.frame_delay = 100
        self.frame_count = 0
        self.sound_freq = 11025
        self.raw_sound_freq = 11025
        self.slots = [0] * HEADER_SLOTS
        self.palette = b"\x00" * 768
        self.frames = []
        self.width = 0
        self.height = 0
        self.audio_chunk_size = 128

    @property
    def audio(self):
        return b"".join(f.audio for f in self.frames)


def _read_chunk(data, pos):
    tag = data[pos:pos + 4]
    (size,) = struct.unpack_from("<I", data, pos + 4)
    return tag, size, pos + 8


def load(path, decode_video=True):
    with open(path, "rb") as f:
        data = f.read()

    tag, _size, pos = _read_chunk(data, 0)
    if tag != b"MOVE":
        raise ValueError("%s: not a PMV movie (%r)" % (path, tag))
    tag, head_size, pos = _read_chunk(data, pos)
    if tag != b"MHED":
        raise ValueError("%s: expected MHED, found %r" % (path, tag))

    movie = PmvMovie()
    (movie.frame_delay,) = struct.unpack_from("<H", data, pos)
    (movie.frame_count,) = struct.unpack_from("<H", data, pos + 6)
    (freq,) = struct.unpack_from("<H", data, pos + 12)
    movie.raw_sound_freq = freq
    movie.sound_freq = {11127: 11025, 22254: 22050}.get(freq, freq)
    movie.slots = list(struct.unpack_from("<%dH" % HEADER_SLOTS, data, pos + 14))
    movie.palette = data[pos + 58:pos + 58 + 768]
    pos += head_size

    palette = bytearray(movie.palette)
    audio_state = resources.SoundDecoderState()
    surface = None

    while pos < len(data) and len(movie.frames) < movie.frame_count:
        tag, size, body = _read_chunk(data, pos)
        if tag != b"MFRM":
            break
        slots = struct.unpack_from("<%dI" % HEADER_SLOTS, data, body)
        frame = PmvFrame(slots[SLOT_FRAME_NUMBER])

        if slots[SLOT_PALETTE]:
            pal_at = body + slots[SLOT_PALETTE] - 8
            (pal_size,) = struct.unpack_from("<I", data, pal_at + 4)
            frame.palette_updates = _decode_palette(data, pal_at + 8, pal_size, palette)

        if slots[SLOT_AUDIO]:
            snd_at = body + slots[SLOT_AUDIO] - 8
            (chunk_size,) = struct.unpack_from("<H", data, snd_at + 4)
            (chunk_count,) = struct.unpack_from("<H", data, snd_at + 6)
            movie.audio_chunk_size = chunk_size
            frame.audio = resources.decompress_sound(
                data, snd_at + 8, chunk_size, chunk_count, audio_state)

        if slots[SLOT_IMAGE]:
            img_at = body + slots[SLOT_IMAGE] - 8
            width, height = struct.unpack_from("<2H", data, img_at + 8)
            cmd_offs, pixel_offs, mask_offs = struct.unpack_from("<3I", data, img_at + 12)
            (line_size,) = struct.unpack_from("<H", data, img_at + 24)
            movie.width, movie.height = width, height
            frame.width, frame.height = width, height
            if decode_video:
                surface = imagecodec.decompress_movie_image(
                    memoryview(data)[img_at:], width, height,
                    cmd_offs, pixel_offs, mask_offs, line_size, prev=surface)
                frame.image = bytes(surface)

        frame.palette = bytes(palette)
        movie.frames.append(frame)
        pos = body + size

    return movie


def _decode_palette(data, pos, size, palette):
    updates = []
    end = pos + size
    while pos < end:
        count = data[pos]
        entry = data[pos + 1]
        pos += 2
        if count == 255 and entry == 255:
            break
        n = (count + 1) * 3
        rgb = bytes(data[pos:pos + n])
        palette[entry * 3:entry * 3 + n] = rgb
        updates.append((entry, rgb))
        pos += n
    return updates


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def save(path, movie, frames, audio=None, palettes=None):
    """Assemble a PMV.

    ``frames``   -- list of ``bytes`` of width*height palette indices
    ``audio``    -- unsigned 8-bit PCM for the whole film, or None
    ``palettes`` -- optional per-frame 768-byte palettes; when a frame's
                    palette differs from the one before it a palette update
                    sub-chunk is written
    """
    width, height = movie.width, movie.height
    chunk_size = movie.audio_chunk_size or 128
    count = len(frames)

    audio_per_frame = []
    if audio:
        total_chunks = (len(audio) + chunk_size - 1) // chunk_size
        per = max(1, (total_chunks + count - 1) // count)
        pos = 0
        for i in range(count):
            take = min(per * chunk_size, len(audio) - pos)
            audio_per_frame.append(audio[pos:pos + take] if take > 0 else b"")
            pos += max(take, 0)
    else:
        audio_per_frame = [b""] * count

    body = bytearray()
    prev_image = None
    prev_palette = palettes[0] if palettes else movie.palette

    for i, image in enumerate(frames):
        slots = [0] * HEADER_SLOTS
        slots[SLOT_FRAME_NUMBER] = i
        sub = bytearray()

        if audio_per_frame[i]:
            chunks, n = resources.encode_sound_chunks(audio_per_frame[i], chunk_size)
            slots[SLOT_AUDIO] = FRAME_HEADER_SIZE + len(sub) + 8
            sub += struct.pack("<IHH", 0, chunk_size, n)
            sub += chunks

        cmd, pixel, mask = imagecodec.compress_movie_image(
            image, width, height, prev_image,
            palettes[i] if palettes else movie.palette)
        cmd_offs = 26
        pixel_offs = cmd_offs + len(cmd)
        mask_offs = pixel_offs + len(pixel)
        total = mask_offs + len(mask)
        slots[SLOT_IMAGE] = FRAME_HEADER_SIZE + len(sub) + 8
        sub += struct.pack("<II2H3IH", total - 4, 0, width, height,
                           cmd_offs, pixel_offs, mask_offs,
                           imagecodec.line_size_for(width))
        sub += cmd + pixel + mask
        prev_image = image

        if palettes and i > 0 and palettes[i] != prev_palette:
            blob = _encode_palette(palettes[i], prev_palette)
            slots[SLOT_PALETTE] = FRAME_HEADER_SIZE + len(sub) + 8
            sub += struct.pack("<II", 0, len(blob)) + blob
            prev_palette = palettes[i]

        payload = struct.pack("<%dI" % HEADER_SLOTS, *slots) + bytes(sub)
        body += b"MFRM" + struct.pack("<I", len(payload)) + payload

    head = bytearray()
    head += struct.pack("<H", movie.frame_delay)
    head += b"\x00" * 4
    head += struct.pack("<H", count)
    head += b"\x00" * 4
    head += struct.pack("<H", movie.raw_sound_freq)
    head += struct.pack("<%dH" % HEADER_SLOTS, *movie.slots)
    head += (palettes[0] if palettes else movie.palette)[:768].ljust(768, b"\x00")

    inner = b"MHED" + struct.pack("<I", len(head)) + bytes(head) + bytes(body)
    with open(path, "wb") as f:
        f.write(b"MOVE" + struct.pack("<I", len(inner)) + inner)
    return len(inner) + 8


def _encode_palette(new, old):
    """Emit runs for the entries that changed."""
    out = bytearray()
    i = 0
    while i < 256:
        if new[i * 3:i * 3 + 3] == old[i * 3:i * 3 + 3]:
            i += 1
            continue
        j = i
        while j < 256 and j - i < 256 and new[j * 3:j * 3 + 3] != old[j * 3:j * 3 + 3]:
            j += 1
        out.append(j - i - 1)
        out.append(i)
        out += new[i * 3:j * 3]
        i = j
    out += b"\xff\xff"
    return bytes(out)


# --------------------------------------------------------------------------
# editable directory tree
# --------------------------------------------------------------------------

def extract(path, outdir, write_frames=True):
    """Unpack a movie into ``frames/*.png``, ``audio.wav`` and ``movie.json``."""
    import json
    import os
    from . import pngio
    from .assets import write_wav

    movie = load(path, decode_video=write_frames)
    os.makedirs(outdir, exist_ok=True)
    frames_dir = os.path.join(outdir, "frames")
    if write_frames:
        os.makedirs(frames_dir, exist_ok=True)

    meta = {
        "tool": "rtztool/1",
        "source": os.path.basename(path),
        "frame_delay": movie.frame_delay,
        "frame_count": len(movie.frames),
        "sound_freq": movie.sound_freq,
        "raw_sound_freq": movie.raw_sound_freq,
        "audio_chunk_size": movie.audio_chunk_size,
        "width": movie.width,
        "height": movie.height,
        "header_slots": movie.slots,
        "frames": [],
    }

    palettes_seen = {}
    for i, frame in enumerate(movie.frames):
        entry = {"number": frame.number}
        if write_frames and frame.image is not None:
            name = "frames/%05d.png" % i
            pngio.write_palette_png(os.path.join(outdir, name),
                                    movie.width, movie.height,
                                    frame.image, frame.palette)
            entry["file"] = name
        if frame.palette_updates:
            entry["palette_changed"] = True
        meta["frames"].append(entry)
        palettes_seen[i] = frame.palette

    audio = movie.audio
    if audio:
        write_wav(os.path.join(outdir, "audio.wav"), audio, movie.sound_freq)
        meta["audio"] = "audio.wav"

    with open(os.path.join(outdir, "movie.json"), "w") as f:
        json.dump(meta, f, indent=1)
    return meta


def build(indir, out_path):
    """Reassemble a movie from an extracted tree."""
    import json
    import os
    from .assets import read_wav, png_to_indices

    with open(os.path.join(indir, "movie.json")) as f:
        meta = json.load(f)

    movie = PmvMovie()
    movie.frame_delay = meta["frame_delay"]
    movie.sound_freq = meta["sound_freq"]
    movie.raw_sound_freq = meta.get("raw_sound_freq", meta["sound_freq"])
    movie.audio_chunk_size = meta.get("audio_chunk_size", 128)
    movie.width = meta["width"]
    movie.height = meta["height"]
    movie.slots = meta.get("header_slots", [0] * HEADER_SLOTS)

    frames = []
    palettes = []
    for entry in meta["frames"]:
        path = os.path.join(indir, entry["file"])
        idx, w, h, pal = png_to_indices(path, movie.palette)
        if (w, h) != (movie.width, movie.height):
            raise ValueError("%s is %dx%d, expected %dx%d"
                             % (entry["file"], w, h, movie.width, movie.height))
        frames.append(idx)
        palettes.append(pal or movie.palette)
    movie.palette = palettes[0]

    audio = b""
    audio_path = os.path.join(indir, meta.get("audio", "audio.wav"))
    if os.path.exists(audio_path):
        audio, _rate = read_wav(audio_path)

    return save(out_path, movie, frames, audio, palettes)
