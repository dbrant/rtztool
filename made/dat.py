"""The MADE game database -- ``RTZCD.DAT``.

This is the file that holds the game itself: every object, every string of text
and every script.  Return to Zork uses the version 3 format.

File header::

     0   6  "ADVSYS"
     6   4  u32 unknown
    10  20  program name, NUL padded ("rtz")
    30   4  u32 offset of the object offset table
    34   2  u16 object count
    36   4  u32 offset of the game state block
    40   4  u32 size of the game state block
    44   4  u32 offset of the constant object block
    48   4  u32 size of the constant object block
    52   2  u16 index of the main code object

The object offset table holds ``object count`` u32 entries.  If the low bit of
an entry is set the object is *constant*: it lives at
``objects_offs + entry - 1`` in the file.  Otherwise it is a *variable* object
and lives at that byte offset inside the game state block, which is the part
that gets written to save games.

An object is::

    0  2  u16 flags        (bit 0 clear => properties are read-only)
    2  2  u16 class        0x7FFF byte array, 0x7FFE word array, else parent object
    4  2  u16 size         for the two array classes
    4  1  u8  count1       for real objects: total property slots
    5  1  u8  count2       for real objects: property ids stored here
    6  .. data

Byte arrays (class 0x7FFF) do double duty: they hold both the game's text and
its compiled scripts.  A script is recognised by ending in opcode 0x1F
(``return``) and disassembling cleanly from the first byte.
"""

import struct

CLASS_BYTE_ARRAY = 0x7FFF
CLASS_WORD_ARRAY = 0x7FFE

HEADER_SIZE = 54


class Object(object):
    __slots__ = ("index", "flags", "cls", "size", "count1", "count2", "data",
                 "constant", "offset")

    def __init__(self, index, flags, cls, raw, constant, offset):
        self.index = index
        self.flags = flags
        self.cls = cls
        self.constant = constant
        self.offset = offset
        if cls == CLASS_BYTE_ARRAY:
            (self.size,) = struct.unpack_from("<H", raw, 4)
            self.count1 = self.count2 = 0
            data_size = self.size
        elif cls == CLASS_WORD_ARRAY:
            (n,) = struct.unpack_from("<H", raw, 4)
            self.size = n
            self.count1 = self.count2 = 0
            data_size = n * 2
        else:
            self.count1 = raw[4]
            self.count2 = raw[5]
            self.size = 0
            data_size = (self.count1 + self.count2) * 2
        self.data = bytes(raw[6:6 + data_size])

    @property
    def total_size(self):
        return 6 + len(self.data)

    @property
    def is_byte_array(self):
        return self.cls == CLASS_BYTE_ARRAY

    @property
    def is_word_array(self):
        return self.cls == CLASS_WORD_ARRAY

    @property
    def is_object(self):
        return self.cls < CLASS_WORD_ARRAY

    def words(self):
        n = len(self.data) // 2
        return list(struct.unpack("<%dH" % n, self.data[:n * 2]))

    def properties(self):
        """(property_id, is_inherited_flag, value) triples for a real object."""
        w = self.words()
        out = []
        for i in range(self.count2):
            pid = w[i]
            value = w[self.count1 + i] if self.count1 + i < len(w) else 0
            out.append((pid & 0x3FFF, pid & 0xC000, value))
        return out

    def text(self, encoding="cp850"):
        raw = self.data
        end = raw.find(b"\x00")
        if end >= 0:
            raw = raw[:end]
        return raw.decode(encoding, "replace")

    def __repr__(self):
        kind = ("bytes" if self.is_byte_array else
                "words" if self.is_word_array else "obj(class %d)" % self.cls)
        return "<Object #%d %s %d bytes%s>" % (
            self.index, kind, len(self.data), "" if self.constant else " var")


class GameDatabase(object):
    def __init__(self):
        self.header_unk = 0
        self.program_name = b""
        self.object_index_offs = 0
        self.game_state_offs = 0
        self.game_state = b""
        self.objects_offs = 0
        self.objects_size = 0
        self.main_code_index = 0
        self.objects = []
        self.object_offsets = []
        self.raw = b""
        self.tail = b""

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            data = f.read()
        return cls.from_bytes(data)

    @classmethod
    def from_bytes(cls, data):
        if data[:6] != b"ADVSYS":
            raise ValueError("not a MADE game database (header %r)" % data[:6])
        self = cls()
        self.raw = data
        (self.header_unk,) = struct.unpack_from("<I", data, 6)
        self.program_name = data[10:30]
        (self.object_index_offs,) = struct.unpack_from("<I", data, 30)
        (object_count,) = struct.unpack_from("<H", data, 34)
        (self.game_state_offs,) = struct.unpack_from("<I", data, 36)
        (game_state_size,) = struct.unpack_from("<I", data, 40)
        (self.objects_offs,) = struct.unpack_from("<I", data, 44)
        (self.objects_size,) = struct.unpack_from("<I", data, 48)
        (self.main_code_index,) = struct.unpack_from("<H", data, 52)

        self.game_state = data[self.game_state_offs:self.game_state_offs + game_state_size]
        self.object_offsets = list(struct.unpack_from(
            "<%dI" % object_count, data, self.object_index_offs))

        for i, off in enumerate(self.object_offsets):
            if off & 1:
                base = self.objects_offs + off - 1
                raw = data[base:base + 6]
                obj = Object(i + 1, *_peek(raw), raw=data[base:base + 6 + _data_size(raw)],
                             constant=True, offset=base)
            else:
                raw = self.game_state[off:off + 6]
                obj = Object(i + 1, *_peek(raw),
                             raw=self.game_state[off:off + 6 + _data_size(raw)],
                             constant=False, offset=off)
            self.objects.append(obj)
        return self

    # ----------------------------------------------------------------- access

    def get(self, index):
        """Objects are 1-based, matching the interpreter."""
        if 1 <= index <= len(self.objects):
            return self.objects[index - 1]
        return None

    def variable(self, index):
        return struct.unpack_from("<h", self.game_state, index * 2)[0]

    def code_objects(self):
        """Indices of byte arrays that look like compiled scripts."""
        from .script import looks_like_code
        return [o.index for o in self.objects
                if o.is_byte_array and looks_like_code(o.data)]

    def text_objects(self, min_len=1):
        """Indices of byte arrays that look like human-readable text."""
        from .script import looks_like_code
        out = []
        for o in self.objects:
            if not o.is_byte_array or len(o.data) < min_len:
                continue
            if looks_like_code(o.data):
                continue
            if _printable_ratio(o.data) >= 0.85:
                out.append(o.index)
        return out

    # ------------------------------------------------------------------ write

    def save(self, path, objects=None):
        """Write the database back out, moving as little as possible.

        A constant object that still fits its original slot is written back
        where it was; one that grew is appended to the end of the constant
        block and its entry in the object offset table is updated.  Variable
        objects live in the game state and never move.

        Keeping unchanged objects in place matters: ScummVM identifies the game
        from the first 5000 bytes of this file, which is mostly the object
        offset table.
        """
        objects = objects if objects is not None else self.objects
        data = self.raw

        block = bytearray(data[self.objects_offs:])
        offsets = list(self.object_offsets)
        state = bytearray(self.game_state)
        moved = []

        for obj in objects:
            body = _pack_object(obj)
            if not obj.constant:
                old = self.game_state[obj.offset:obj.offset + 6]
                if len(body) != 6 + _data_size(old):
                    raise ValueError(
                        "object %d lives in the game state and cannot change size"
                        % obj.index)
                state[obj.offset:obj.offset + len(body)] = body
                continue
            rel = obj.offset - self.objects_offs
            old_size = 6 + _data_size(data[obj.offset:obj.offset + 6])
            if len(body) <= old_size:
                block[rel:rel + len(body)] = body
                offsets[obj.index - 1] = rel + 1
            else:
                if len(block) & 1:
                    block += b"\x00"
                offsets[obj.index - 1] = len(block) + 1
                block += body
                moved.append(obj.index)

        out = bytearray(data[:self.object_index_offs])
        out += struct.pack("<%dI" % len(offsets), *offsets)
        out += data[len(out):self.game_state_offs]
        out += state
        out += data[len(out):self.objects_offs]
        out += block

        if moved:
            struct.pack_into("<I", out, 48, len(block))
        with open(path, "wb") as f:
            f.write(bytes(out))
        return len(out), moved


def _peek(raw):
    flags, cls = struct.unpack_from("<HH", raw, 0)
    return flags, cls


def _data_size(raw):
    (cls,) = struct.unpack_from("<H", raw, 2)
    if cls == CLASS_BYTE_ARRAY:
        return struct.unpack_from("<H", raw, 4)[0]
    if cls == CLASS_WORD_ARRAY:
        return struct.unpack_from("<H", raw, 4)[0] * 2
    return (raw[4] + raw[5]) * 2


def _pack_object(obj):
    out = bytearray(struct.pack("<HH", obj.flags, obj.cls))
    if obj.cls == CLASS_BYTE_ARRAY:
        out += struct.pack("<H", len(obj.data))
    elif obj.cls == CLASS_WORD_ARRAY:
        out += struct.pack("<H", len(obj.data) // 2)
    else:
        out += bytes((obj.count1, obj.count2))
    out += obj.data
    return bytes(out)


def _printable_ratio(data):
    if not data:
        return 0.0
    good = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13, 0))
    return good / float(len(data))
