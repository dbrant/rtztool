"""Reader for MADE ``.RED`` archives (``RTZCD.RED``, ``DRIVERS.RED``).

A RED file is a plain sequence of entries::

    0   8  unknown
    8   4  u32 compressed size
   12   4  u32 uncompressed size
   16  10  unknown
   26  13  file name, NUL terminated
   39   2  unknown
   41  ..  compressed data

The payload is LZH (LHA ``-lh5-`` style: 13-bit dictionary, dynamic Huffman)
with one wrinkle -- the byte stream is broken into 0xFFE-byte blocks separated
by two bytes the decoder skips.

Only decompression is implemented.  If you need to change ``RTZCD.DAT`` for a
game that ships it inside a RED, use the loose copy from the CD instead (see
the README): the engine's "installed" layout reads ``RTZCD.DAT`` directly.
"""

import struct

__all__ = ["RedEntry", "list_files", "extract_file"]

ENTRY_HEADER = 41

BITBUFSIZ = 16
DICBIT = 13
DICSIZ = 1 << DICBIT
MATCHBIT = 8
MAXMATCH = 256
THRESHOLD = 3
NC = 255 + MAXMATCH + 2 - THRESHOLD      # 510
CBIT = 9
NP = DICBIT + 1                          # 14
NT = 16 + 3                              # 19
PBIT = 4
TBIT = 5
NPT = NT


class RedEntry(object):
    __slots__ = ("name", "comp_size", "orig_size", "data_offset", "header")

    def __init__(self, name, comp_size, orig_size, data_offset, header):
        self.name = name
        self.comp_size = comp_size
        self.orig_size = orig_size
        self.data_offset = data_offset
        self.header = header

    def __repr__(self):
        return "<RedEntry %s %d->%d>" % (self.name, self.comp_size, self.orig_size)


def list_files(path):
    with open(path, "rb") as f:
        data = f.read()
    out = []
    pos = 0
    while pos + ENTRY_HEADER <= len(data):
        comp_size, orig_size = struct.unpack_from("<II", data, pos + 8)
        name = data[pos + 26:pos + 39].split(b"\x00")[0].decode("latin-1")
        if not name or comp_size == 0:
            break
        out.append(RedEntry(name, comp_size, orig_size, pos + ENTRY_HEADER,
                            bytes(data[pos:pos + ENTRY_HEADER])))
        pos += ENTRY_HEADER + comp_size
    return out


def extract_file(path, name):
    with open(path, "rb") as f:
        data = f.read()
    for entry in list_files(path):
        if entry.name.upper() == name.upper():
            return _LzhDecoder(data, entry.data_offset, entry.comp_size).run(entry.orig_size)
    raise KeyError("%s does not contain %r" % (path, name))


class _LzhDecoder(object):
    def __init__(self, data, offset, comp_size):
        self._data = data
        self._pos = offset
        self._comp_size = comp_size
        self._block_pos = 0

        self._bitbuf = 0
        self._subbitbuf = 0
        self._bitcount = 0
        self._left = [0] * (2 * NC - 1)
        self._right = [0] * (2 * NC - 1)
        self._c_len = bytearray(NC)
        self._pt_len = bytearray(NPT)
        self._c_table = [0] * 4096
        self._pt_table = [0] * 256
        self._blocksize = 0
        self._decode_i = 0
        self._decode_j = 0

    # -- bit reader -------------------------------------------------------

    def _read_byte(self):
        if self._block_pos == 0xFFE:
            self._block_pos = 0
            self._pos += 2
        b = self._data[self._pos]
        self._pos += 1
        self._block_pos += 1
        return b

    def _fillbuf(self, count):
        self._bitbuf = (self._bitbuf << count) & 0xFFFF
        while count > self._bitcount:
            count -= self._bitcount
            self._bitbuf = (self._bitbuf | (self._subbitbuf << count)) & 0xFFFF
            if self._comp_size != 0:
                self._comp_size -= 1
                self._subbitbuf = self._read_byte()
            else:
                self._subbitbuf = 0
            self._bitcount = 8
        self._bitcount -= count
        self._bitbuf = (self._bitbuf | (self._subbitbuf >> self._bitcount)) & 0xFFFF

    def _getbits(self, count):
        x = self._bitbuf >> (BITBUFSIZ - count)
        self._fillbuf(count)
        return x

    # -- huffman ----------------------------------------------------------

    def _make_table(self, nchar, bitlen, tablebits, table):
        count = [0] * 17
        weight = [0] * 17
        start = [0] * 18
        for i in range(nchar):
            count[bitlen[i]] += 1
        start[1] = 0
        # start[] is 16-bit in the original, so the total wraps back to zero
        for i in range(1, 17):
            start[i + 1] = (start[i] + (count[i] << (16 - i))) & 0xFFFF
        if start[17] != 0:
            raise ValueError("corrupt LZH stream (bad Huffman table)")
        jutbits = 16 - tablebits
        for i in range(1, tablebits + 1):
            start[i] >>= jutbits
            weight[i] = 1 << (tablebits - i)
        for i in range(tablebits + 1, 17):
            weight[i] = 1 << (16 - i)
        i = start[tablebits + 1] >> jutbits
        if i != 0:
            k = 1 << tablebits
            while i != k:
                table[i] = 0
                i += 1
        avail = nchar
        mask = 1 << (15 - tablebits)
        for ch in range(nchar):
            length = bitlen[ch]
            if length == 0:
                continue
            nextcode = start[length] + weight[length]
            if length <= tablebits:
                for i in range(start[length], nextcode):
                    table[i] = ch
            else:
                k = start[length]
                idx = k >> jutbits
                container = table
                i = length - tablebits
                while i != 0:
                    if container[idx] == 0:
                        self._right[avail] = 0
                        self._left[avail] = 0
                        container[idx] = avail
                        avail += 1
                    if k & mask:
                        idx = container[idx]
                        container = self._right
                    else:
                        idx = container[idx]
                        container = self._left
                    k = (k << 1) & 0xFFFF
                    i -= 1
                container[idx] = ch
            start[length] = nextcode & 0xFFFF

    def _read_pt_len(self, nn, nbit, i_special):
        v = self._getbits(nbit)
        if v == 0:
            c = self._getbits(nbit)
            for i in range(nn):
                self._pt_len[i] = 0
            for i in range(256):
                self._pt_table[i] = c
        else:
            i = 0
            while i < v:
                c = self._bitbuf >> (BITBUFSIZ - 3)
                if c == 7:
                    mask = 1 << (BITBUFSIZ - 1 - 3)
                    while mask & self._bitbuf:
                        mask >>= 1
                        c += 1
                self._fillbuf(3 if c < 7 else c - 3)
                self._pt_len[i] = c
                i += 1
                if i == i_special:
                    c = self._getbits(2)
                    while c > 0:
                        self._pt_len[i] = 0
                        i += 1
                        c -= 1
            while i < nn:
                self._pt_len[i] = 0
                i += 1
            self._make_table(nn, self._pt_len, 8, self._pt_table)

    def _read_c_len(self):
        v = self._getbits(CBIT)
        if v == 0:
            c = self._getbits(CBIT)
            for i in range(NC):
                self._c_len[i] = 0
            for i in range(4096):
                self._c_table[i] = c
        else:
            i = 0
            while i < v:
                c = self._pt_table[self._bitbuf >> (BITBUFSIZ - 8)]
                if c >= NT:
                    mask = 1 << (BITBUFSIZ - 1 - 8)
                    while c >= NT:
                        c = self._right[c] if (self._bitbuf & mask) else self._left[c]
                        mask >>= 1
                self._fillbuf(self._pt_len[c])
                if c <= 2:
                    if c == 0:
                        c = 1
                    elif c == 1:
                        c = self._getbits(4) + 3
                    else:
                        c = self._getbits(CBIT) + 20
                    while c > 0:
                        self._c_len[i] = 0
                        i += 1
                        c -= 1
                else:
                    self._c_len[i] = c - 2
                    i += 1
            while i < NC:
                self._c_len[i] = 0
                i += 1
            self._make_table(NC, self._c_len, 12, self._c_table)

    def _decode_c(self):
        if self._blocksize == 0:
            self._blocksize = self._getbits(16)
            self._read_pt_len(NT, TBIT, 3)
            self._read_c_len()
            self._read_pt_len(NP, PBIT, -1)
        self._blocksize -= 1
        j = self._c_table[self._bitbuf >> (BITBUFSIZ - 12)]
        if j >= NC:
            mask = 1 << (BITBUFSIZ - 1 - 12)
            while j >= NC:
                j = self._right[j] if (self._bitbuf & mask) else self._left[j]
                mask >>= 1
        self._fillbuf(self._c_len[j])
        return j

    def _decode_p(self):
        j = self._pt_table[self._bitbuf >> (BITBUFSIZ - 8)]
        if j >= NP:
            mask = 1 << (BITBUFSIZ - 1 - 8)
            while j >= NP:
                j = self._right[j] if (self._bitbuf & mask) else self._left[j]
                mask >>= 1
        self._fillbuf(self._pt_len[j])
        if j != 0:
            j = (1 << (j - 1)) + self._getbits(j - 1)
        return j

    # -- driver -----------------------------------------------------------

    def run(self, orig_size):
        self._bitbuf = 0
        self._subbitbuf = 0
        self._bitcount = 0
        self._fillbuf(BITBUFSIZ)
        self._blocksize = 0
        self._decode_j = 0

        window = bytearray(DICSIZ)
        out = bytearray()
        remaining = orig_size
        while remaining > 0:
            n = DICSIZ if remaining > DICSIZ else remaining
            self._decode_into(n, window)
            out += window[:n]
            remaining -= n
        return bytes(out)

    def _decode_into(self, count, buf):
        r = 0
        while self._decode_j > 0:
            self._decode_j -= 1
            buf[r] = buf[self._decode_i]
            self._decode_i = (self._decode_i + 1) & (DICSIZ - 1)
            r += 1
            if r == count:
                return
        while True:
            c = self._decode_c()
            if c <= 255:
                buf[r] = c
                r += 1
                if r == count:
                    return
            else:
                self._decode_j = c - (255 + 1 - THRESHOLD)
                self._decode_i = (r - self._decode_p() - 1) & (DICSIZ - 1)
                while self._decode_j > 0:
                    self._decode_j -= 1
                    buf[r] = buf[self._decode_i]
                    self._decode_i = (self._decode_i + 1) & (DICSIZ - 1)
                    r += 1
                    if r == count:
                        return
