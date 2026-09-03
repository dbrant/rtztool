"""Reader/writer for MADE engine ``.PRJ`` resource archives (Return to Zork).

File layout
-----------

::

    offset  size  meaning
    0x00       4  "PROJ"
    0x04       4  u32  file size (may lag the real size on shipped discs)
    0x08       2  u16  unknown (354)
    0x0A       2  u16  unknown (2614)
    0x0C       4  "DDIT"
    0x10       4  u32  unknown / directory scratch
    0x14       4  u32  unknown / directory scratch
    0x18       2  u16  number of resource-type directory entries
    0x1A     24*N  directory entries

Each 24-byte directory entry:

::

    0   4  resource type tag, e.g. "FLEX"
    4   4  u32 offset of that type's INDX chunk
    8   4  u32 total size of the INDX chunk (header included)
   12   4  u32 offset of that type's SYMB chunk (0 if none)
   16   4  u32 total size of the SYMB chunk
   20   2  u16 62  -- size of the per-resource DATA header
   22   2  u16 58  -- unknown, constant

INDX chunk (the offset/size table for one resource type)::

    0   4  "INDX"
    4   4  u32 payload size (chunk size minus these 8 bytes)
    8   4  u32 checksum: sum of every byte from offset 12 to end of chunk
   12   4  resource type tag
   16   2  u16 capacity (slots allocated)
   18   2  u16 count    (slots in use, including the dummy slot 0)
   20   2  u16 count again (for FREE this is always 1)
   22   8*capacity  (u32 offset, u32 size) pairs; slot 0 is a dummy

SYMB chunk (names for one resource type)::

    0   4  "SYMB"
    4   4  u32 payload size
    8   4  u32 checksum (same rule as INDX)
   12   4  resource type tag
   16   2  u16 capacity
   18   2  u16 count (number of named resources)
   20   2  u16 unknown (0)
   22  18*capacity  (char[16] name, u16 resource index)

Every resource begins with a 62-byte DATA header, then the payload::

    0   4  "DATA"
    4   4  u32 chunk size == slot size - 8
    8   4  u32 checksum: sum of every byte from offset 12 to end of the slot
   12   4  resource type tag
   16   8  creation timestamp blob (constant across the shipped archive)
   24   2  u16 resource index
   26   2  u16 flags (always 1)
   28   2  u16 unknown (0)
   30  16  char[16] name, NUL padded
   46  16  char[16] name again
   62  ..  payload

The engine reads a resource as ``slot.offset + 62`` for ``slot.size - 62``
bytes, so the payload is what the individual codecs see.
"""

import struct

MAGIC = b"PROJ"
RES_HEADER_SIZE = 62
INDX_ENTRY_SIZE = 8
SYMB_ENTRY_SIZE = 18
NAME_SIZE = 16

# Resource types that hold no payload data; they exist only as bookkeeping.
BOOKKEEPING_TYPES = (b"FREE", b"ARCH", b"OMNI")


def _checksum(buf):
    return sum(buf) & 0xFFFFFFFF


class Resource(object):
    """One entry of the archive."""

    __slots__ = ("type", "index", "name", "flags", "unknown", "timestamp", "payload",
                 "src_offset", "src_size")

    def __init__(self, type_, index, name, payload, flags=1, unknown=0,
                 timestamp=b"\x00" * 8, src_offset=0, src_size=0):
        self.type = type_
        self.index = index
        self.name = name
        self.payload = payload
        self.flags = flags
        self.unknown = unknown
        self.timestamp = timestamp
        self.src_offset = src_offset
        self.src_size = src_size

    def __repr__(self):
        return "<Resource %s #%d %r %d bytes>" % (
            self.type.decode("ascii"), self.index, self.name, len(self.payload))

    def header_bytes(self):
        name = self.name.encode("ascii", "replace")[:NAME_SIZE]
        name = name + b"\x00" * (NAME_SIZE - len(name))
        return (b"DATA"
                + struct.pack("<I", RES_HEADER_SIZE - 8 + len(self.payload))
                + b"\x00\x00\x00\x00"          # checksum, filled in later
                + self.type
                + self.timestamp
                + struct.pack("<HHH", self.index, self.flags, self.unknown)
                + name + name)

    def to_bytes(self):
        hdr = bytearray(self.header_bytes())
        body = hdr[12:] + self.payload
        struct.pack_into("<I", hdr, 8, _checksum(body))
        return bytes(hdr) + self.payload


class FreeBlock(object):
    __slots__ = ("offset", "size")

    def __init__(self, offset, size):
        self.offset = offset
        self.size = size


class ProjectFile(object):
    """A parsed ``.PRJ`` archive."""

    def __init__(self):
        self.header_unk1 = 354
        self.header_unk2 = 2614
        self.ddit_a = 0
        self.ddit_b = 0
        self.type_order = []           # list of type tags, archive order
        self.resources = {}            # tag -> {index: Resource}
        self.capacities = {}           # tag -> INDX capacity from the source file
        self.symb_capacities = {}      # tag -> SYMB capacity
        self.has_symb = {}             # tag -> bool
        self.free_blocks = []
        self.arch_present = False
        self._data = None

    # ---------------------------------------------------------------- reading

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            data = f.read()
        return cls.from_bytes(data)

    @classmethod
    def from_bytes(cls, data):
        if data[:4] != MAGIC:
            raise ValueError("not a MADE .PRJ archive (bad magic %r)" % data[:4])
        self = cls()
        self._data = data
        self.header_unk1, self.header_unk2 = struct.unpack_from("<HH", data, 8)
        self.ddit_a, self.ddit_b = struct.unpack_from("<II", data, 0x10)
        (count,) = struct.unpack_from("<H", data, 0x18)

        entries = []
        off = 0x1A
        for _ in range(count):
            tag = data[off:off + 4]
            indx_off, indx_size, symb_off, symb_size, hdr_size, unk = \
                struct.unpack_from("<IIIIHH", data, off + 4)
            entries.append((tag, indx_off, indx_size, symb_off, symb_size, hdr_size))
            off += 24

        for tag, indx_off, indx_size, symb_off, symb_size, hdr_size in entries:
            self.type_order.append(tag)
            slots, capacity = _read_indx(data, indx_off, tag)
            self.capacities[tag] = capacity
            names = {}
            if symb_off:
                names, symb_cap = _read_symb(data, symb_off, tag)
                self.symb_capacities[tag] = symb_cap
                self.has_symb[tag] = True
            else:
                self.has_symb[tag] = False

            if tag == b"FREE":
                self.free_blocks = [FreeBlock(o, s) for o, s in slots[1:] if s]
                continue
            if tag == b"ARCH":
                self.arch_present = True
                continue

            table = {}
            for index in range(1, len(slots)):
                offset, size = slots[index]
                if size == 0:
                    continue
                if data[offset:offset + 4] != b"DATA":
                    raise ValueError("%s slot %d at 0x%X is not a DATA chunk"
                                     % (tag.decode(), index, offset))
                rtype = data[offset + 12:offset + 16]
                timestamp = data[offset + 16:offset + 24]
                r_index, flags, unknown = struct.unpack_from("<HHH", data, offset + 24)
                name = data[offset + 30:offset + 46].rstrip(b"\x00").decode("latin-1")
                if not name:
                    name = names.get(index, "")
                payload = data[offset + RES_HEADER_SIZE:offset + size]
                table[index] = Resource(rtype, index, name, payload, flags, unknown,
                                        timestamp, offset, size)
            self.resources[tag] = table
        return self

    # ---------------------------------------------------------------- helpers

    def types(self):
        return [t for t in self.type_order if t not in BOOKKEEPING_TYPES]

    def get(self, tag, index):
        return self.resources.get(tag, {}).get(index)

    def by_name(self, tag, name):
        for res in self.resources.get(tag, {}).values():
            if res.name == name:
                return res
        return None

    def iter_resources(self):
        for tag in self.types():
            for index in sorted(self.resources.get(tag, {})):
                yield self.resources[tag][index]

    # ---------------------------------------------------------------- writing

    def save(self, path, compact=True):
        """Write the archive back out.

        ``compact`` rebuilds the file from scratch (no free space, resources in
        index order).  The result is a clean archive that the engine reads
        exactly like the original.
        """
        out = bytearray()
        types = [t for t in self.type_order if t not in (b"FREE", b"OMNI")]
        if b"FREE" in self.type_order:
            types.append(b"FREE")

        dir_size = 0x1A + 24 * len(types)
        # Keep the original 0x1A..0x400 gap so offsets stay comfortably clear of
        # the header; the shipped archive does the same.
        cursor = max(dir_size, 0x400)
        out += b"\x00" * cursor

        # --- resource payloads
        placements = {}   # tag -> {index: (offset, size)}
        for tag in types:
            if tag in BOOKKEEPING_TYPES:
                continue
            table = self.resources.get(tag, {})
            placed = {}
            for index in sorted(table):
                res = table[index]
                res.index = index
                blob = res.to_bytes()
                placed[index] = (cursor, len(blob))
                out += blob
                cursor += len(blob)
                if cursor & 1:          # keep chunks 2-byte aligned
                    out += b"\x00"
                    cursor += 1
            placements[tag] = placed

        # --- INDX and SYMB chunks
        dir_entries = []
        for tag in types:
            if tag == b"FREE":
                indx_off = cursor
                blob = _build_indx(tag, {}, capacity=self.capacities.get(tag, 64),
                                   count2_is_one=True)
                out += blob
                cursor += len(blob)
                dir_entries.append((tag, indx_off, len(blob), 0, 0))
                continue
            if tag == b"ARCH":
                indx_off = cursor
                blob = _build_indx(tag, {}, capacity=self.capacities.get(tag, 50))
                out += blob
                cursor += len(blob)
                symb_off = symb_size = 0
                if self.has_symb.get(tag):
                    symb_off = cursor
                    sblob = _build_symb(tag, {}, self.symb_capacities.get(tag, 50))
                    out += sblob
                    cursor += len(sblob)
                    symb_size = len(sblob)
                dir_entries.append((tag, indx_off, len(blob), symb_off, symb_size))
                continue

            placed = placements.get(tag, {})
            capacity = max(self.capacities.get(tag, 0), (max(placed) + 1) if placed else 1)
            indx_off = cursor
            blob = _build_indx(tag, placed, capacity)
            out += blob
            cursor += len(blob)

            symb_off = symb_size = 0
            if self.has_symb.get(tag, True):
                names = {i: self.resources[tag][i].name for i in placed
                         if self.resources[tag][i].name}
                symb_cap = max(self.symb_capacities.get(tag, 0), capacity)
                symb_off = cursor
                sblob = _build_symb(tag, names, symb_cap)
                out += sblob
                cursor += len(sblob)
                symb_size = len(sblob)
            dir_entries.append((tag, indx_off, len(blob), symb_off, symb_size))

        # --- fixed header + directory
        head = bytearray()
        head += MAGIC
        head += struct.pack("<I", len(out))
        head += struct.pack("<HH", self.header_unk1, self.header_unk2)
        head += b"DDIT"
        head += struct.pack("<II", self.ddit_a, self.ddit_b)
        head += struct.pack("<H", len(dir_entries))
        for tag, io, isz, so, ssz in dir_entries:
            head += tag + struct.pack("<IIIIHH", io, isz, so, ssz, RES_HEADER_SIZE, 58)
        out[0:len(head)] = head

        with open(path, "wb") as f:
            f.write(bytes(out))
        return len(out)


def _read_indx(data, offset, expect_tag):
    if data[offset:offset + 4] != b"INDX":
        raise ValueError("expected INDX at 0x%X, found %r" % (offset, data[offset:offset + 4]))
    (size,) = struct.unpack_from("<I", data, offset + 4)
    (stored_sum,) = struct.unpack_from("<I", data, offset + 8)
    tag = data[offset + 12:offset + 16]
    if tag != expect_tag:
        raise ValueError("INDX at 0x%X is for %r, expected %r" % (offset, tag, expect_tag))
    capacity, count, _count2 = struct.unpack_from("<HHH", data, offset + 16)
    base = offset + 22
    slots = [struct.unpack_from("<II", data, base + 8 * i) for i in range(count)]
    return slots, capacity


def _read_symb(data, offset, expect_tag):
    if data[offset:offset + 4] != b"SYMB":
        raise ValueError("expected SYMB at 0x%X, found %r" % (offset, data[offset:offset + 4]))
    capacity, count, _unk = struct.unpack_from("<HHH", data, offset + 16)
    base = offset + 22
    names = {}
    for i in range(count):
        raw = data[base + 18 * i:base + 18 * i + NAME_SIZE]
        (index,) = struct.unpack_from("<H", data, base + 18 * i + NAME_SIZE)
        names[index] = raw.rstrip(b"\x00").decode("latin-1")
    return names, capacity


def _build_indx(tag, placed, capacity, count2_is_one=False):
    count = (max(placed) + 1) if placed else 1
    capacity = max(capacity, count)
    body = bytearray()
    body += tag
    body += struct.pack("<HHH", capacity, count, 1 if count2_is_one else count)
    for i in range(capacity):
        offset, size = placed.get(i, (0, 0))
        body += struct.pack("<II", offset, size)
    chunk = bytearray(b"INDX")
    chunk += struct.pack("<I", len(body) + 8)
    chunk += struct.pack("<I", _checksum(body))
    chunk += body
    return bytes(chunk)


def _build_symb(tag, names, capacity):
    items = sorted(names.items())
    items.sort(key=lambda kv: kv[1].upper())    # the archive keeps names sorted
    capacity = max(capacity, len(items))
    body = bytearray()
    body += tag
    body += struct.pack("<HHH", capacity, len(items), 0)
    for index, name in items:
        raw = name.encode("latin-1")[:NAME_SIZE]
        body += raw + b"\x00" * (NAME_SIZE - len(raw))
        body += struct.pack("<H", index)
    body += b"\x00" * (SYMB_ENTRY_SIZE * (capacity - len(items)))
    chunk = bytearray(b"SYMB")
    chunk += struct.pack("<I", len(body) + 8)
    chunk += struct.pack("<I", _checksum(body))
    chunk += body
    return bytes(chunk)


# --------------------------------------------------------------------------
# in-place patching
# --------------------------------------------------------------------------

class PatchResult(object):
    def __init__(self):
        self.in_place = 0
        self.appended = 0
        self.index_moved = []
        self.header_changed = False
        self.size = 0


def patch_archive(src_path, out_path, replacements, names=None):
    """Rewrite an archive with some resources replaced, touching as little as
    possible.

    ``replacements`` maps ``{type_tag: {index: payload}}``.  A resource whose
    new DATA chunk still fits its old slot is written where it was; anything
    bigger is appended to the end of the file.  Each type's INDX (and SYMB)
    chunk is rewritten in place because its capacity -- and therefore its size
    -- does not change.

    Keeping the header and the index chunks at their original offsets means the
    first kilobytes of the file are untouched, which is what ScummVM hashes to
    recognise the game.
    """
    with open(src_path, "rb") as f:
        data = bytearray(f.read())
    result = PatchResult()

    (dir_count,) = struct.unpack_from("<H", data, 0x18)
    directory = {}
    for i in range(dir_count):
        off = 0x1A + 24 * i
        tag = bytes(data[off:off + 4])
        indx_off, indx_size, symb_off, symb_size, _hs, _u = \
            struct.unpack_from("<IIIIHH", data, off + 4)
        directory[tag] = (off, indx_off, indx_size, symb_off, symb_size)

    for tag, items in replacements.items():
        if tag not in directory:
            raise KeyError("archive has no %r section" % tag)
        dir_off, indx_off, indx_size, symb_off, symb_size = directory[tag]
        capacity, count, count2 = struct.unpack_from("<HHH", data, indx_off + 16)
        base = indx_off + 22
        slots = [list(struct.unpack_from("<II", data, base + 8 * i)) for i in range(capacity)]

        for index, payload in sorted(items.items()):
            if index >= capacity:
                raise ValueError("%s index %d exceeds the archive's capacity of %d"
                                 % (tag.decode(), index, capacity))
            old_off, old_size = slots[index]
            template = data[old_off:old_off + RES_HEADER_SIZE] if old_size else None
            blob = _rebuild_chunk(template, tag, index, payload,
                                  (names or {}).get(tag, {}).get(index))
            if old_size and len(blob) <= old_size:
                data[old_off:old_off + len(blob)] = blob
                slots[index] = [old_off, len(blob)]
                result.in_place += 1
            else:
                if len(data) & 1:
                    data += b"\x00"
                slots[index] = [len(data), len(blob)]
                data += blob
                result.appended += 1
            count = max(count, index + 1)

        for i, (o, s) in enumerate(slots):
            struct.pack_into("<II", data, base + 8 * i, o, s)
        struct.pack_into("<HHH", data, indx_off + 16, capacity, count,
                         1 if tag == b"FREE" else count)
        body = data[indx_off + 12:indx_off + 8 + indx_size]
        struct.pack_into("<I", data, indx_off + 8, _checksum(body))

    with open(out_path, "wb") as f:
        f.write(bytes(data))
    result.size = len(data)
    return result


def _rebuild_chunk(template, tag, index, payload, name=None):
    """Wrap a payload in a DATA header, reusing the old header where possible."""
    hdr = bytearray(template) if template else bytearray(RES_HEADER_SIZE)
    if not template:
        hdr[0:4] = b"DATA"
        hdr[12:16] = tag
        struct.pack_into("<HHH", hdr, 24, index, 1, 0)
    if name is not None:
        raw = name.encode("latin-1")[:NAME_SIZE]
        raw = raw + b"\x00" * (NAME_SIZE - len(raw))
        hdr[30:46] = raw
        hdr[46:62] = raw
    struct.pack_into("<I", hdr, 4, RES_HEADER_SIZE - 8 + len(payload))
    struct.pack_into("<H", hdr, 24, index)
    blob = bytes(hdr) + payload
    struct.pack_into("<I", hdr, 8, _checksum(blob[12:]))
    return bytes(hdr) + payload
