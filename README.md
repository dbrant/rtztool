# rtztool — unpack and repack Return to Zork

Return to Zork (Activision, 1993) runs on Activision's **MADE** engine. Its data
lives in four file formats, all of which this toolkit reads *and* writes:

| File | What's in it | Tool |
|---|---|---|
| `RTZCD.PRJ` | every picture, sprite animation, sound, music track, font and in-game text string | `rtztool prj` |
| `RTZCD.DAT` | the game itself — objects, properties and ~4100 compiled scripts | `rtztool dat` |
| `*.PMV` | the full-motion cutscenes (video + audio) | `rtztool pmv` |
| `RTZCD.RED` | LZH archive holding `RTZCD.DAT` and the DOS executables | `rtztool red` |

Requires Python 3.8+ and nothing else — no third-party packages.

## Quick start

```sh
# look around
python3 rtztool.py prj info    RTZ-CD/RTZCD.PRJ
python3 rtztool.py prj list    RTZ-CD/RTZCD.PRJ --type MENU

# unpack everything into editable files (~80 MB, about 15 seconds)
python3 rtztool.py prj extract RTZ-CD/RTZCD.PRJ mod/

# ... edit mod/FLEX/*.png, mod/MENU/*.txt, mod/SNDS/*.wav ...

# repack; only the files you touched are re-encoded
python3 rtztool.py prj build   mod/ RTZCD.PRJ --source RTZ-CD/RTZCD.PRJ
```

`build` patches a copy of the original archive rather than rewriting it, so an
untouched round trip is byte-for-byte identical and a small edit produces a
small diff. Pass `--compact` if you would rather have a freshly laid-out file.

### What ends up in `mod/`

```
mod/manifest.json          every resource: index, name, flags, checksums
mod/FLEX/<NAME>.png        1783 still pictures, 8-bit palettized PNG
mod/ANIM/<NAME>/*.png      246 sprite animations, one PNG per frame
mod/ANIM/<NAME>/anim.json  frame list and flags
mod/SNDS/<NAME>.wav        1249 speech and effect clips, 8-bit mono
mod/XMID/<NAME>.xmi        206 XMIDI music tracks, untouched
mod/MENU/<NAME>.txt        49 string tables — this is the game's text
mod/FONT/<NAME>.png        29 glyph sheets, 16 glyphs per row
mod/FONT/<NAME>.json       per-glyph advance widths
```

`manifest.json` records a SHA-256 for each extracted file. At build time only
the files whose hash changed are re-encoded; everything else is copied straight
out of the source archive, so nothing degrades through repeated round trips.

### Where the game's text is

Almost all visible prose — descriptions, dialogue, verbs, the notebook, the
endgame — is in the 49 `MENU` resources (1401 strings, ~46 000 characters). The
scripts pull them in at runtime with `sfReadMenu(object, menu, index)`. Edit
`mod/MENU/*.txt`, one string per line, and rebuild. `RTZCD.DAT` holds only about
570 further strings, mostly debug output, `printf` templates, save file names and
the `.PMV` filenames.

### Editing pictures

The PNGs are 8-bit palettized. If your editor preserves the palette and indices
(most do for indexed PNGs), the pixels come back exactly. If you save RGB or
RGBA instead, colours are matched to the picture's original palette. Changing a
picture's PLTE changes the palette the game loads with that picture.

The codec stores each 4×4 block as one of: a solid colour, two colours plus a
1-bit mask, four colours plus a 2-bit mask, or 16 literal bytes. The encoder
picks whichever fits, so any image round-trips losslessly. `--lossy` disables the
literal mode and reduces busy blocks to four colours instead, matching what the
original tools did and producing slightly smaller data.

### Sounds

Extracted as 8-bit unsigned mono WAV at 11025 Hz (`--rate` to change the rate
written into the header; the engine's own rate is set by script). Replacements
are re-encoded as uncompressed chunks, which is bit-exact but roughly 3× the
size of the original ADPCM-style encoding. That is fine for a handful of
replacements; replacing all 26 MB of speech would roughly triple the archive.

## The game database

```sh
python3 rtztool.py dat info    RTZ-CD/RTZCD.DAT
python3 rtztool.py dat extract RTZ-CD/RTZCD.DAT game/
python3 rtztool.py dat disasm  RTZ-CD/RTZCD.DAT 457    # the main loop
python3 rtztool.py dat build   game/ RTZCD.DAT --source RTZ-CD/RTZCD.DAT
```

`dat extract` writes `database.json` (the object table), `strings.json`,
`objects.txt` (a readable dump of the object hierarchy and its properties) and
one `.asm` file per script under `scripts/`. Re-assembling all 4135 scripts
untouched reproduces the original file byte for byte.

The disassembly looks like this — `dat disasm` annotates constants that point at
text objects:

```
.script 1898
    tspace          2
    push
    load_constant   20
    call            0
    extend          sfAllocTimer 0
    set             88
L0025:
    load_variable   19
    branch_false    L0766
```

Branch operands are absolute offsets into the script; the assembler resolves
`Lxxxx` labels back to the same numbers, so you can insert and delete
instructions freely.

## Cutscenes

```sh
python3 rtztool.py pmv info    FWIZ01X1.PMV
python3 rtztool.py pmv extract FINTRO00.PMV intro/     # frames/*.png + audio.wav
python3 rtztool.py pmv build   intro/ FINTRO00.PMV
```

Frames come out as full pictures even though the format only stores changes
between them, and the audio comes out as one continuous WAV. Rebuilding
re-derives the inter-frame deltas, so an untouched rebuild is pixel-identical
and sample-identical (and usually smaller than the original).

## A note on ScummVM detection

ScummVM identifies a game from the MD5 of the **first 5000 bytes** of each data
file. `prj build` and `dat build` are written to leave that region alone
wherever possible — the archive header, index chunks and object table stay
where they are and changed data is appended — and every build prints whether the
fingerprint survived. If it did, ScummVM keeps recognising the game. If it
didn't (which happens when a database object grows and has to be relocated),
ScummVM will call it an unknown variant until the new checksum is added to
`engines/made/detection_tables.h`.

The `RTZ-CD/` directory on its own (`RTZCD.DAT` + `RTZCD.PRJ`, with the `.PMV`
files alongside) is a complete ScummVM game directory for the "installed CD"
layout, which reads `RTZCD.DAT` directly. That is the easiest target for mods —
no need to rebuild `RTZCD.RED`, which this toolkit only reads.

---

# The file formats

Everything below was recovered by inspecting the shipped data; ScummVM's MADE
engine was used as a cross-check for the parts it also implements. All integers
are little endian unless stated otherwise. Chunk tags are 4 ASCII bytes.

## RTZCD.PRJ — the resource archive

### Header

```
0x00   4  "PROJ"
0x04   4  u32  file size (stale in shipped copies — 8 bytes short)
0x08   2  u16  354
0x0A   2  u16  2614
0x0C   4  "DDIT"
0x10   4  u32  directory scratch
0x14   4  u32  directory scratch
0x18   2  u16  number of resource-type entries (8)
0x1A  24  per type, see below
```

Directory entry, 24 bytes:

```
 0   4  type tag: FREE ARCH FONT FLEX XMID SNDS ANIM MENU
 4   4  u32 offset of this type's INDX chunk
 8   4  u32 total size of that chunk
12   4  u32 offset of this type's SYMB chunk (0 if none)
16   4  u32 total size of that chunk
20   2  u16 62 — the size of a resource's DATA header
22   2  u16 58 — constant, purpose unknown
```

`FREE` is the archive's free-space list and `ARCH` is empty in the shipped file;
the other six hold the actual data.

### INDX — the offset table for one type

```
 0   4  "INDX"
 4   4  u32 payload size (total chunk size minus 8)
 8   4  u32 checksum: the sum of every byte from offset 12 to the end
12   4  the type tag again
16   2  u16 capacity — how many slots are allocated
18   2  u16 count — how many are in use, including the dummy slot 0
20   2  u16 count again (always 1 for FREE)
22   8  per slot: u32 file offset, u32 size — capacity of them
```

Slot 0 is a dummy, so resource indices are 1-based, matching the interpreter.
Slots past `count` hold stale data from earlier builds.

### SYMB — names for one type

```
 0   4  "SYMB"
 4   4  u32 payload size
 8   4  u32 checksum, same rule as INDX
12   4  the type tag
16   2  u16 capacity
18   2  u16 count
20   2  u16 0
22  18  per entry: char[16] name (NUL padded), u16 resource index
```

Entries are sorted by name. The names also appear inside each resource's own
header, twice, so `SYMB` is really a lookup index.

### Resource header — 62 bytes before every payload

```
 0   4  "DATA"
 4   4  u32 chunk size == slot size - 8
 8   4  u32 checksum: sum of every byte from offset 12 to the end of the slot
12   4  the type tag
16   8  creation timestamp, identical across the shipped archive
24   2  u16 resource index
26   2  u16 flags (always 1)
28   2  u16 0
30  16  char[16] name
46  16  char[16] the same name again
62  ..  payload
```

The engine reads a resource by seeking to `slot.offset + 62` and reading
`slot.size - 62` bytes, so the payload is what the codecs below see.

### FLEX — still pictures

```
 0   1  u8  1 if a palette follows
 1   3  u8  cmd / pixel / mask stream flags (bit 1 = 4-bit nibble mode)
 4   2  u16 offset of the command stream
 6   2  u16 offset of the pixel stream
 8   2  u16 offset of the mask stream
10   2  u16 command bytes per row of blocks
12   2  u16 number of block rows == ceil(height / 4)
14   2  u16 width
16   2  u16 height
18   .  palette, 3 bytes per entry, (cmd_offset - 18) / 3 entries
        (always 256 in this game; 8 bits per channel, not VGA's 6)
```

then the three streams. Return to Zork always uses flags 0 — plain bytes.

### The 4×4 block codec

The picture is cut into 4×4 blocks, left to right then top to bottom in bands of
four scan lines. Each block gets a 2-bit command; commands are packed four per
byte, low bits first, `line_size = ceil(ceil(width / 4) / 4)` bytes per band.

| cmd | pixel stream | mask stream | meaning |
|---|---|---|---|
| 0 | 1 byte | — | solid colour |
| 1 | 2 bytes | u16 | 1 bit per pixel selects between them |
| 2 | 4 bytes | u32 | 2 bits per pixel |
| 3 | — | 16 bytes | 16 literal pixels (in a still picture) |
| 3 | — | — | leave the block unchanged (in a delta frame) |

The decoder reads the command bits 16 at a time. The *last* 16-bit word of a
band is special: it supplies only `(ceil(width/4) & 7) or 8` commands, and
because `line_size` may be odd that final read can run one byte past the band —
the original reads whatever stale byte is in its stack buffer, and those bits are
never used. Deriving the last word's position needs the original's rounding:
`last = (((line_size + 1) >> 1) << 1) - 2`.

The decompressor also assembles each band in a scratch buffer with a hard-coded
stride of 320, which is why no picture is wider than the screen.

### ANIM — sprite animations

```
 0   4  "ANIM"
 4   4  u32 payload size - 8
 8   2  u16 100
10   2  u16 flags — bit 0 means the frames are deltas
12   2  u16 width
14   2  u16 height
16   4  u32 0
20   2  u16 frame count
22   4  u16, u16
26   4  u32 file offset of each frame
```

Each frame:

```
 0   4  u32 frame size - 4
 4   4  u32 0
 8   2  u16 width
10   2  u16 height
12   4  u32 command stream offset (26, i.e. straight after this header)
16   4  u32 pixel stream offset
20   4  u32 mask stream offset
24   2  u16 command bytes per row of blocks
26   .  the three streams
```

Frames carry no palette; animations are drawn against whatever palette the
current picture loaded. In a delta animation, pixel value 0 means transparent
and command 3 skips the block entirely.

### SNDS — speech and effects

```
 0   4  "SFLX"
 4   4  u32 payload size - 4
 8   4  u32 chunk count
12   2  u16 samples per chunk (128)
14   .  the chunks
```

Each chunk starts with one byte: the low nibble is the coding type and the top
two bits are a resampling factor (1 = the chunk holds half as many samples and
is linearly interpolated back up, 2 = a quarter).

| type | body |
|---|---|
| 0 | nothing — silence, and the running sample resets |
| 1 | nothing — repeat the previous chunk |
| 2 | a 2-entry delta table, then 8 samples per byte (1 bit each) |
| 3 | a 4-entry delta table, then 4 samples per byte (2 bits each) |
| 4 | a 16-entry delta table, then 2 samples per byte (4 bits each) |
| 5 | uncompressed 8-bit unsigned PCM |

Delta table entries are stored as `(value + 64) / 2`; the running sample is
clipped to ±127 and biased by 128 on output. Types 2–4 are a per-chunk ADPCM:
the codebook is re-sent with every chunk. Output is 8-bit unsigned mono.

### MENU — string tables

```
0   4  "MENU"
4   2  u16 string count
6   2  u16 offset of each string, from the start of the payload
.   .  NUL-terminated strings, code page 850
```

### FONT — 1bpp bitmap fonts

```
0   1  u8 glyph height
1   .  per character: u8 advance width, then `height` rows of 8 pixels,
       one byte per row, most significant bit leftmost
```

Character codes start at 28. How many are stored varies by font (99, 190, 228 or
229 in the shipped archive) and is implied by the payload length.

### XMID — music

Raw XMIDI (`FORM`/`XDIR`/`CAT `/`XMID`), untouched by the archive.

## RTZCD.DAT — the game database

Header:

```
 0   6  "ADVSYS"
 6   4  u32 unknown
10  20  program name, "rtz"
30   4  u32 offset of the object offset table
34   2  u16 object count (11044)
36   4  u32 offset of the game state block
40   4  u32 size of the game state block
44   4  u32 offset of the constant object block
48   4  u32 size of that block
52   2  u16 index of the main code object (457)
```

The offset table holds one u32 per object. **If the low bit is set** the object
is constant and lives at `constant_block + value - 1` in the file; otherwise it
is a variable object living at that byte offset inside the game state — the
block that save games write out. Objects are 1-based.

An object:

```
0   2  u16 flags — bit 0 clear means its properties are read-only
2   2  u16 class
4   2  u16 size            (when class is 0x7FFF or 0x7FFE)
4   1  u8  count1          (otherwise: total property slots)
5   1  u8  count2          (otherwise: property ids stored on this object)
6   .   data
```

Class `0x7FFF` is a byte array, `0x7FFE` a word array, and anything smaller is
the index of the parent object — that is how the class hierarchy is stored.
A real object's data is `count2` property ids followed by `count1` values;
property lookup walks up the parent chain. Ids are masked with `0x3FFF`; bit 14
means the value is an index into the game state rather than a literal, and bit
15 marks a slot that isn't inherited positionally.

Byte arrays do double duty: they hold both text and compiled scripts. A script
is recognisable because its last byte is `0x1F` (`return`) and it disassembles
cleanly from byte 0 — that heuristic finds 4135 scripts in 4710 byte arrays,
and re-assembling all of them reproduces the file exactly.

### The bytecode

One byte per opcode, operands following immediately. `w`/`W` are u16, `b` is u8,
`E` is an index into the external function table.

```
 1 branch_true W     16 lt                31 return           46 objectp
 2 branch_false W    17 eq                32 call b           47 vectorp
 3 branch W          18 gt                33 svar             48 restart
 4 true              19 load_constant w   34 sset             49 rand
 5 false             20 load_variable w   35 split            50 randomize
 6 push              21 get_property      36 snlit            51 send b
 7 not               22 set_property      37 yorn             52 extend E b
 8 add               23 set w             38 save             53 catch
 9 sub               24 print             39 restore          54 cdone
10 mul               25 terpri            40 arg b            55 throw
11 div               26 print_number      41 aset b           56 functionp
12 mod               27 vref              42 tmp b            57 le
13 band              28 vset              43 tset b           58 ge
14 bor               29 vsize             44 tspace b         59 varx
15 bnot              30 exit              45 class            60 setx
```

It is a stack machine. Branch operands are absolute offsets into the script.
`extend` calls one of 100 engine functions — graphics, sound, sprites, timers,
text, CD audio, movie playback — listed in `made/script.py`.

## *.PMV — cutscenes

A flat chunk stream; each chunk is a 4-byte **big-endian** tag and a u32 size.
`MOVE` wraps the file, `MHED` is the header, and one `MFRM` follows per frame.

```
MHED:
 0   2  u16 frame delay, milliseconds
 2   4  unused
 6   2  u16 frame count
 8   4  unused
12   2  u16 sound rate — 11127 and 22254 are really 11025 and 22050
14  44  22 u16 values, one per stream slot
58 768  the starting palette
```

```
MFRM payload:
 0  88  22 u32 stream offsets. Slot 0 is the frame number, slot 2 the audio
        sub-chunk, slot 3 the image, slot 4 a palette update. Offsets count
        from the start of the MFRM chunk including its 8-byte header.
88   .  the sub-chunks
```

The image sub-chunk uses the same 26-byte header as an ANIM frame and the same
block codec, with two differences: blocks are addressed by a cursor that walks
the whole frame rather than being clipped per band, and command 3 means "this
block is unchanged since the previous frame". That inter-frame prediction is
what keeps the videos small. The audio sub-chunk is `u32 0, u16 samples per
chunk, u16 chunk count` followed by exactly the chunk format `SNDS` uses — with
the decoder's state carried across frames for the whole film. A palette update
is a u32 size then runs of `count, first index, RGB × (count + 1)`, terminated
by `FF FF`.

## RTZCD.RED — the installer archive

A plain sequence of entries:

```
 0   8  unknown
 8   4  u32 compressed size
12   4  u32 uncompressed size
16  10  unknown
26  13  file name, NUL terminated
39   2  unknown
41   .  compressed data
```

The payload is LZH — LHA's `-lh5-` scheme with a 13-bit dictionary and dynamic
Huffman coding — with one quirk: the byte stream is cut into 0xFFE-byte blocks
separated by two bytes the decoder skips. `rtztool red extract` reproduces every
file bit-exactly; writing RED archives is not implemented.

## Layout of this toolkit

```
rtztool.py            the command line front end
made/prj.py           .PRJ container: read, rewrite, in-place patch
made/imagecodec.py    the 4x4 block codec, both directions, both variants
made/resources.py     FLEX / ANIM / SNDS / MENU / FONT codecs
made/assets.py        .PRJ <-> editable directory tree
made/dat.py           the game database
made/script.py        bytecode disassembler and assembler
made/datassets.py     .DAT <-> editable directory tree
made/pmv.py           cutscenes
made/red.py           LZH archive reader
made/pngio.py         a small dependency-free PNG reader/writer
```
