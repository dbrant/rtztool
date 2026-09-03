"""Turn a ``.PRJ`` archive into an editable directory tree, and back again.

Layout produced by :func:`extract`::

    <outdir>/manifest.json
    <outdir>/FLEX/<NAME>.png          still pictures
    <outdir>/ANIM/<NAME>/frame00.png  animation frames
    <outdir>/ANIM/<NAME>/anim.json    animation metadata
    <outdir>/SNDS/<NAME>.wav          8-bit unsigned mono PCM
    <outdir>/XMID/<NAME>.xmi          XMIDI music, untouched
    <outdir>/MENU/<NAME>.txt          one string per line
    <outdir>/FONT/<NAME>.png          glyph sheet, 16 cells per row
    <outdir>/FONT/<NAME>.json         glyph advance widths

:func:`build` walks the manifest and re-encodes only the files whose contents
changed; everything else is copied verbatim out of the original archive, so an
untouched rebuild is a faithful copy of the game's data.
"""

import hashlib
import json
import os
import re
import struct

from . import pngio
from . import resources
from .prj import ProjectFile, Resource, patch_archive

MANIFEST = "manifest.json"
DEFAULT_SOUND_RATE = 11025

_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def _safe_name(name, index):
    out = _SAFE.sub("_", name) or ("res%04d" % index)
    return out


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _greyscale_palette():
    return bytes(bytearray([v for i in range(256) for v in (i, i, i)]))


def pick_reference_palette(prj, name=None):
    """A palette used to display resources that carry none of their own."""
    if name:
        res = prj.by_name(b"FLEX", name)
        if res is None:
            raise KeyError("no FLEX resource named %r" % name)
        pic = resources.decode_picture(res.payload)
        if pic.palette:
            return pic.palette, name
    for index in sorted(prj.resources.get(b"FLEX", {})):
        res = prj.resources[b"FLEX"][index]
        try:
            pic = resources.decode_picture(res.payload)
        except Exception:
            continue
        if pic.palette and pic.width >= 320:
            return pic.palette, res.name
    return _greyscale_palette(), None


# --------------------------------------------------------------------------
# WAV
# --------------------------------------------------------------------------

def write_wav(path, samples, rate):
    n = len(samples)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate, 1, 8))
        f.write(b"data" + struct.pack("<I", n))
        f.write(samples)


def read_wav(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("%s: not a RIFF/WAVE file" % path)
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        body = data[pos + 8:pos + 8 + size]
        if tag == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", body, 0)
        elif tag == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("%s: missing fmt or data chunk" % path)
    audio_format, channels, rate, _br, _ba, bits = fmt
    if audio_format != 1:
        raise ValueError("%s: only uncompressed PCM is supported" % path)
    if bits == 8:
        samples = pcm
    elif bits == 16:
        n = len(pcm) // 2
        vals = struct.unpack("<%dh" % n, pcm[:n * 2])
        samples = bytes((v >> 8) + 128 & 0xFF for v in vals)
    else:
        raise ValueError("%s: unsupported bit depth %d" % (path, bits))
    if channels > 1:
        samples = samples[::channels]
    return samples, rate


# --------------------------------------------------------------------------
# palette mapping
# --------------------------------------------------------------------------

class PaletteMapper(object):
    """Maps arbitrary RGB values onto a fixed 256-colour palette."""

    def __init__(self, palette):
        self.palette = palette
        self._cache = {}

    def index_of(self, r, g, b):
        key = (r << 16) | (g << 8) | b
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        pal = self.palette
        best = 0
        bestd = 1 << 30
        for i in range(256):
            dr = pal[i * 3] - r
            dg = pal[i * 3 + 1] - g
            db = pal[i * 3 + 2] - b
            d = dr * dr + dg * dg + db * db
            if d < bestd:
                bestd = d
                best = i
                if d == 0:
                    break
        self._cache[key] = best
        return best

    def map_rgba(self, rgba, count, transparent_index=None):
        out = bytearray(count)
        for i in range(count):
            r, g, b, a = rgba[i * 4:i * 4 + 4]
            if transparent_index is not None and a < 128:
                out[i] = transparent_index
            else:
                out[i] = self.index_of(r, g, b)
        return out


def png_to_indices(path, palette, transparent_index=None):
    """Read a PNG back into palette indices plus, maybe, a replacement palette.

    Returns ``(indices, width, height, new_palette_or_None)``.  When the file is
    a 256-entry palettized PNG its indices are used verbatim and its PLTE is
    returned as the new palette; anything else is colour-matched against
    ``palette``.
    """
    img = pngio.read_png(path)
    if img.is_palettized and img.palette is not None and len(img.palette) >= 768:
        return bytes(img.pixels), img.width, img.height, bytes(img.palette[:768])
    rgba = img.to_rgba()
    mapper = PaletteMapper(palette)
    idx = mapper.map_rgba(rgba, img.width * img.height, transparent_index)
    new_pal = None
    if img.is_palettized and img.palette:
        new_pal = bytes(img.palette) + b"\x00" * (768 - len(img.palette))
    return bytes(idx), img.width, img.height, new_pal


# --------------------------------------------------------------------------
# font sheets
# --------------------------------------------------------------------------

FONT_COLS = 16


def font_to_png(font, path):
    rows = (font.char_count + FONT_COLS - 1) // FONT_COLS
    w = FONT_COLS * 8
    h = rows * font.height
    buf = bytearray(w * h)
    for i in range(font.char_count):
        code = resources.FONT_FIRST_CHAR + i
        _width, glyph = font.glyphs.get(code, (0, [0] * font.height))
        cx = (i % FONT_COLS) * 8
        cy = (i // FONT_COLS) * font.height
        for y, bits in enumerate(glyph):
            base = (cy + y) * w + cx
            for x in range(8):
                if bits & (0x80 >> x):
                    buf[base + x] = 1
    pal = bytearray(768)
    pal[3:6] = b"\xff\xff\xff"
    pngio.write_palette_png(path, w, h, bytes(buf), bytes(pal))


def png_to_font(path, height, char_count, widths):
    img = pngio.read_png(path)
    w = img.width
    if img.is_palettized:
        pix = img.pixels
        def on(i):
            return pix[i] != 0
    else:
        rgba = img.to_rgba()
        def on(i):
            return rgba[i * 4 + 3] >= 128 and (rgba[i * 4] + rgba[i * 4 + 1] + rgba[i * 4 + 2]) > 127
    glyphs = {}
    for i in range(char_count):
        cx = (i % FONT_COLS) * 8
        cy = (i // FONT_COLS) * height
        rows = []
        for y in range(height):
            bits = 0
            for x in range(8):
                px = cx + x
                py = cy + y
                if px < img.width and py < img.height and on(py * w + px):
                    bits |= 0x80 >> x
            rows.append(bits)
        glyphs[resources.FONT_FIRST_CHAR + i] = (widths[i], rows)
    return resources.Font(height, glyphs, char_count)


# --------------------------------------------------------------------------
# menu text files
# --------------------------------------------------------------------------

def menu_to_text(menu):
    return "".join(
        s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r") + "\n"
        for s in menu.strings)


def text_to_menu(text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out = []
    for line in lines:
        out.append(line.replace("\\n", "\n").replace("\\r", "\r").replace("\\\\", "\\"))
    return resources.Menu(out)


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------

def extract(prj_path, outdir, sound_rate=DEFAULT_SOUND_RATE, palette_name=None,
            only_types=None, progress=None):
    prj = ProjectFile.load(prj_path)
    ref_palette, ref_name = pick_reference_palette(prj, palette_name)

    manifest = {
        "tool": "rtztool/1",
        "source": os.path.basename(prj_path),
        "source_size": os.path.getsize(prj_path),
        "sound_rate": sound_rate,
        "reference_palette": ref_name,
        "header": {
            "unk1": prj.header_unk1,
            "unk2": prj.header_unk2,
            "ddit_a": prj.ddit_a,
            "ddit_b": prj.ddit_b,
            "type_order": [t.decode("ascii") for t in prj.type_order],
        },
        "types": {},
    }
    os.makedirs(outdir, exist_ok=True)

    for tag in prj.types():
        name = tag.decode("ascii")
        if only_types and name not in only_types:
            continue
        entries = []
        used = set()
        os.makedirs(os.path.join(outdir, name), exist_ok=True)
        for index in sorted(prj.resources[tag]):
            res = prj.resources[tag][index]
            base = _safe_name(res.name, index)
            if base.upper() in used:
                base = "%s_%04d" % (base, index)
            used.add(base.upper())

            entry = {
                "index": index,
                "name": res.name,
                "flags": res.flags,
                "unknown": res.unknown,
                "timestamp": res.timestamp.hex(),
                "src_offset": res.src_offset,
                "src_size": res.src_size,
            }
            try:
                files = _extract_one(tag, res, outdir, name, base, entry,
                                     ref_palette, sound_rate)
            except Exception as exc:                       # noqa: BLE001
                path = os.path.join(outdir, name, base + ".bin")
                with open(path, "wb") as f:
                    f.write(res.payload)
                entry["format"] = "raw"
                entry["error"] = str(exc)
                files = [os.path.relpath(path, outdir)]
            entry["files"] = [p.replace(os.sep, "/") for p in files]
            entry["sha256"] = {
                p.replace(os.sep, "/"): _sha256(os.path.join(outdir, p)) for p in files
            }
            entries.append(entry)
            if progress:
                progress(name, index, res.name)

        manifest["types"][name] = {
            "capacity": prj.capacities.get(tag, 0),
            "symb_capacity": prj.symb_capacities.get(tag, 0),
            "has_symb": prj.has_symb.get(tag, True),
            "resources": entries,
        }

    with open(os.path.join(outdir, MANIFEST), "w") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def _extract_one(tag, res, outdir, type_dir, base, entry, ref_palette, sound_rate):
    rel = lambda *p: os.path.join(type_dir, *p)

    if tag == b"FLEX":
        pic = resources.decode_picture(res.payload)
        entry["format"] = "picture"
        entry["width"] = pic.width
        entry["height"] = pic.height
        entry["has_palette"] = pic.palette is not None
        path = rel(base + ".png")
        pngio.write_palette_png(os.path.join(outdir, path), pic.width, pic.height,
                                pic.indices, pic.palette or ref_palette)
        return [path]

    if tag == b"ANIM":
        anim = resources.decode_anim(res.payload, accumulate=False)
        entry["format"] = "animation"
        entry["width"] = anim.width
        entry["height"] = anim.height
        entry["flags"] = anim.flags
        os.makedirs(os.path.join(outdir, type_dir, base), exist_ok=True)
        files = []
        meta = {
            "width": anim.width,
            "height": anim.height,
            "flags": anim.flags,
            "delta": anim.is_delta,
            "header_unk": anim.header_unk,
            "frames": [],
        }
        for i, frame in enumerate(anim.frames):
            path = rel(base, "frame%03d.png" % i)
            pngio.write_palette_png(
                os.path.join(outdir, path), frame.width, frame.height,
                frame.indices, ref_palette, transparent_index=0)
            meta["frames"].append({"file": "frame%03d.png" % i,
                                   "width": frame.width, "height": frame.height,
                                   "unk1": frame.unk1})
            files.append(path)
        mpath = rel(base, "anim.json")
        with open(os.path.join(outdir, mpath), "w") as f:
            json.dump(meta, f, indent=1)
        files.append(mpath)
        return files

    if tag == b"SNDS":
        snd = resources.decode_sound(res.payload)
        entry["format"] = "sound"
        entry["chunk_size"] = snd.chunk_size
        entry["samples"] = len(snd.samples)
        path = rel(base + ".wav")
        write_wav(os.path.join(outdir, path), snd.samples, sound_rate)
        return [path]

    if tag == b"MENU":
        menu = resources.decode_menu(res.payload)
        entry["format"] = "menu"
        entry["count"] = len(menu.strings)
        path = rel(base + ".txt")
        with open(os.path.join(outdir, path), "w", encoding="utf-8") as f:
            f.write(menu_to_text(menu))
        return [path]

    if tag == b"FONT":
        font = resources.decode_font(res.payload)
        entry["format"] = "font"
        entry["height"] = font.height
        entry["char_count"] = font.char_count
        png = rel(base + ".png")
        font_to_png(font, os.path.join(outdir, png))
        js = rel(base + ".json")
        widths = [font.glyphs[resources.FONT_FIRST_CHAR + i][0]
                  for i in range(font.char_count)]
        with open(os.path.join(outdir, js), "w") as f:
            json.dump({"height": font.height, "first_char": resources.FONT_FIRST_CHAR,
                       "char_count": font.char_count, "widths": widths}, f, indent=1)
        return [png, js]

    if tag == b"XMID":
        entry["format"] = "xmidi"
        path = rel(base + ".xmi")
        with open(os.path.join(outdir, path), "wb") as f:
            f.write(res.payload)
        return [path]

    entry["format"] = "raw"
    path = rel(base + ".bin")
    with open(os.path.join(outdir, path), "wb") as f:
        f.write(res.payload)
    return [path]


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(indir, source_prj, out_prj, allow_raw=True, progress=None, force=False,
          compact=False):
    """Rebuild an archive from an extracted tree.

    Only resources whose files changed are re-encoded.  By default the original
    archive is patched in place (changed resources are written back over their
    old slot when they still fit, appended otherwise), which leaves the file
    header and index chunks exactly where they were.  ``compact=True`` rewrites
    the whole archive instead, dropping stale data at the cost of a completely
    new file layout.
    """
    with open(os.path.join(indir, MANIFEST)) as f:
        manifest = json.load(f)

    src = ProjectFile.load(source_prj)
    ref_palette, _ = pick_reference_palette(src, manifest.get("reference_palette"))

    changes = {}
    names = {}
    changed = 0
    for type_name, info in manifest["types"].items():
        tag = type_name.encode("ascii")
        for entry in info["resources"]:
            index = entry["index"]
            dirty = force
            for rel in entry["files"]:
                path = os.path.join(indir, rel)
                if not os.path.exists(path):
                    raise IOError("missing asset file %s" % rel)
                if _sha256(path) != entry["sha256"].get(rel):
                    dirty = True
            if not dirty:
                continue
            payload = _encode_one(tag, indir, entry, ref_palette, allow_raw,
                                  src.get(tag, index))
            changes.setdefault(tag, {})[index] = payload
            names.setdefault(tag, {})[index] = entry["name"]
            changed += 1
            if progress:
                progress(type_name, index, entry["name"], True)

    if compact:
        out = ProjectFile()
        out.header_unk1 = manifest["header"]["unk1"]
        out.header_unk2 = manifest["header"]["unk2"]
        out.ddit_a = manifest["header"]["ddit_a"]
        out.ddit_b = manifest["header"]["ddit_b"]
        out.type_order = [t.encode("ascii") for t in manifest["header"]["type_order"]]
        for tag in src.type_order:
            out.capacities[tag] = src.capacities.get(tag, 0)
            out.symb_capacities[tag] = src.symb_capacities.get(tag, 0)
            out.has_symb[tag] = src.has_symb.get(tag, True)
        for tag in src.types():
            table = dict(src.resources[tag])
            for index, payload in changes.get(tag, {}).items():
                if index in table:
                    table[index].payload = payload
                else:
                    table[index] = Resource(tag, index,
                                            names.get(tag, {}).get(index, ""), payload)
            out.resources[tag] = table
        size = out.save(out_prj)
        return changed, size, None

    result = patch_archive(source_prj, out_prj, changes, names)
    return changed, result.size, result


def _encode_one(tag, indir, entry, ref_palette, allow_raw, original=None):
    fmt = entry.get("format", "raw")
    files = entry["files"]

    if fmt == "picture":
        path = os.path.join(indir, files[0])
        # colour-match against whatever palette this picture originally used,
        # so an RGB edit lands on the same colours the artwork already had
        base_palette = ref_palette
        if original is not None:
            try:
                base_palette = resources.decode_picture(original.payload).palette or ref_palette
            except Exception:                                   # noqa: BLE001
                pass
        idx, w, h, new_pal = png_to_indices(path, base_palette)
        palette = None
        if entry.get("has_palette", True):
            palette = new_pal or base_palette
        pic = resources.Picture(w, h, idx, palette)
        return resources.encode_picture(pic, allow_raw=allow_raw)

    if fmt == "animation":
        meta_path = os.path.join(indir, files[-1])
        with open(meta_path) as f:
            meta = json.load(f)
        frames = []
        for fi in meta["frames"]:
            path = os.path.join(indir, os.path.dirname(files[0]), fi["file"])
            idx, w, h, _pal = png_to_indices(path, ref_palette, transparent_index=0)
            frames.append(resources.AnimFrame(w, h, idx, 0, fi.get("unk1", 0)))
        anim = resources.Animation(meta["width"], meta["height"], meta["flags"],
                                   frames, meta.get("header_unk"))
        return resources.encode_anim(anim, ref_palette, allow_raw=allow_raw)

    if fmt == "sound":
        samples, _rate = read_wav(os.path.join(indir, files[0]))
        return resources.encode_sound(
            resources.Sound(samples, entry.get("chunk_size", 128)))

    if fmt == "menu":
        with open(os.path.join(indir, files[0]), encoding="utf-8") as f:
            text = f.read()
        return resources.encode_menu(text_to_menu(text))

    if fmt == "font":
        with open(os.path.join(indir, files[1])) as f:
            meta = json.load(f)
        font = png_to_font(os.path.join(indir, files[0]), meta["height"],
                           meta["char_count"], meta["widths"])
        return resources.encode_font(font)

    with open(os.path.join(indir, files[0]), "rb") as f:
        return f.read()
