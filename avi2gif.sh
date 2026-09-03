#!/usr/bin/env bash
# avi2gif.sh - convert a screen-capture AVI (DOSBox ZMBV & friends) into an
# animated GIF containing only the unique frames, each held for exactly as long
# as it was on screen in the source.
#
#   * consecutive frames are deduplicated by exact hash, never by a fuzzy
#     similarity threshold, so no genuinely distinct frame is ever dropped;
#   * frame delays come from rounding each frame's CUMULATIVE start time to
#     centiseconds, so rounding error never accumulates over the animation;
#   * if the source uses <= 256 colours the palette is taken verbatim from the
#     source and the GIF is verified bit-identical to the input frames.
#
# Usage: avi2gif.sh [options] <input.avi> [output.gif]
set -euo pipefail

find_tool() {
  local n=$1
  if command -v "$n" >/dev/null 2>&1; then command -v "$n"; return; fi
  for c in "/c/Tools/ffmpeg/$n.exe" "/c/Program Files/ffmpeg/bin/$n.exe"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  echo "error: $n not found; set FFMPEG/FFPROBE or add it to PATH" >&2; exit 1
}
FFMPEG=${FFMPEG:-$(find_tool ffmpeg)}
FFPROBE=${FFPROBE:-$(find_tool ffprobe)}
command -v python >/dev/null 2>&1 || { echo "error: python not found" >&2; exit 1; }

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'

Options:
  -o FILE            output path (default: <input>.gif)
  -s WxH             resize, e.g. -s 640x480
  -a W:H             resize to this display aspect, e.g. -a 4:3
                     (640x400 -> 640x480; corrects DOS non-square pixels)
  --smooth           use lanczos when resizing (default: nearest-neighbour,
                     which keeps pixel art crisp and the palette exact)
  --fps N            resample to N fps before deduplicating (fewer, coarser
                     frames; useful when a capture has little repetition)
  --min-delay N      minimum per-frame delay in centiseconds (default 2).
                     Browsers silently promote a delay of 0 or 1cs to 10cs,
                     so going below 2 is almost never what you want.
  --dither MODE      dithering used only when the source needs >256 colours
                     (default sierra2_4a; "none" suits flat pixel art)
  --loop N           0 = loop forever (default), -1 = play once
  --keep-temp        keep the working directory and print its path
  -h, --help         this help
USAGE
  exit "${1:-0}"
}

OUT=""; SIZE=""; ASPECT=""; FPS=""; MIN_DELAY=2; LOOP=0
DITHER="sierra2_4a"; SWS="neighbor"; KEEP=0; IN=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT=$2; shift 2;;
    -s) SIZE=$2; shift 2;;
    -a) ASPECT=$2; shift 2;;
    --smooth) SWS="lanczos"; shift;;
    --fps) FPS=$2; shift 2;;
    --min-delay) MIN_DELAY=$2; shift 2;;
    --dither) DITHER=$2; shift 2;;
    --loop) LOOP=$2; shift 2;;
    --keep-temp) KEEP=1; shift;;
    -h|--help) usage 0;;
    -*) echo "unknown option: $1" >&2; usage 1;;
    *) if [ -z "$IN" ]; then IN=$1; elif [ -z "$OUT" ]; then OUT=$1; else
         echo "unexpected argument: $1" >&2; usage 1; fi; shift;;
  esac
done
[ -n "$IN" ] || usage 1
[ -f "$IN" ] || { echo "error: no such file: $IN" >&2; exit 1; }
[ -n "$OUT" ] || OUT="${IN%.*}.gif"

# absolute paths, because everything below runs with cwd inside the temp dir
winpath() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m -a "$1"
  else case "$1" in /*) echo "$1";; *) echo "$PWD/$1";; esac; fi
}
IN_W=$(winpath "$IN")
mkdir -p "$(dirname "$OUT")"
OUT_W=$(winpath "$OUT")

# ---- source geometry, and the filter chain used identically in both passes ----
IFS=, read -r SW SH < <("$FFPROBE" -v error -select_streams v:0 \
                          -show_entries stream=width,height -of csv=p=0 "$IN_W")
W=$SW; H=$SH
if [ -n "$SIZE" ]; then
  W=${SIZE%%x*}; H=${SIZE##*x}
elif [ -n "$ASPECT" ]; then
  AW=${ASPECT%%:*}; AH=${ASPECT##*:}
  H=$(( (SW * AH + AW / 2) / AW ))
  H=$(( H % 2 ? H + 1 : H ))
fi
FILTER=""
[ -n "$FPS" ] && FILTER="fps=$FPS,"
[ "$W:$H" != "$SW:$SH" ] && FILTER="${FILTER}scale=$W:$H:flags=$SWS,"
FILTER="${FILTER}format=rgb24"

TMP=$(mktemp -d)
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$TMP"; }
trap cleanup EXIT
cd "$TMP"; mkdir uniq

# ---------------------------------------------------------------- pass helpers
cat > runs.py <<'PYEOF'
import re, sys
md5, min_delay = sys.argv[1], int(sys.argv[2])
tb_n = tb_d = None; frames = []
for line in open(md5):
    if line.startswith('#'):
        m = re.match(r'#tb \d+: (\d+)/(\d+)', line)
        if m: tb_n, tb_d = int(m.group(1)), int(m.group(2))
        continue
    f = [x.strip() for x in line.split(',')]
    if len(f) >= 6: frames.append((int(f[2]), int(f[3]), f[5]))   # pts, duration, hash
if not frames: sys.exit('no frames decoded')
if tb_n is None: sys.exit('could not read the time base from framemd5 output')

base = frames[0][0]
def cs(ticks):                                  # ticks -> centiseconds, rounded
    return (ticks * tb_n * 200 + tb_d) // (tb_d * 2)
end = cs(frames[-1][0] + max(frames[-1][1], 1) - base)

runs = []                                       # [index of first frame, start in cs]
for i, (pts, _, h) in enumerate(frames):
    if not runs or h != frames[i - 1][2]: runs.append([i, cs(pts - base)])

delays = []; clamped = 0
for k, (idx, start) in enumerate(runs):
    d = (runs[k + 1][1] if k + 1 < len(runs) else end) - start
    if d < min_delay: d = min_delay; clamped += 1
    delays.append(d)

with open('keep.txt', 'w', newline='\n') as f:
    f.write('\n'.join(str(r[0]) for r in runs) + '\n')
with open('delays.txt', 'w', newline='\n') as f:
    for d in delays: f.write('%d\n' % d)
with open('list.txt', 'w', newline='\n') as f:                # concat demuxer timing script.
    for i, d in enumerate(delays):              # "option framerate 100" forces the
        f.write("file 'uniq/f%06d.png'\n" % i)  # inner demuxer onto a 1/100s time
        f.write("option framerate 100\n")       # base; without it the durations
        f.write("duration %.2f\n" % (d / 100.0))# quantise to 1/25s and collapse.
    f.write("file 'uniq/f%06d.png'\noption framerate 100\n" % (len(delays) - 1))
with open('vars.sh', 'w', newline='\n') as f:
    f.write('N_TOTAL=%d\nN_UNIQUE=%d\nLAST_DELAY=%d\nTOTAL_CS=%d\nSRC_CS=%d\nCLAMPED=%d\n'
            % (len(frames), len(runs), delays[-1], sum(delays), end, clamped))
PYEOF

cat > extract.py <<'PYEOF'
import struct, sys, zlib
W, H = int(sys.argv[1]), int(sys.argv[2])
FS = W * H * 3
keep = [int(l) for l in open('keep.txt')]
want = set(keep); pos = dict((n, i) for i, n in enumerate(keep))

def png(path, data):
    rows = b''.join(b'\x00' + data[y * W * 3:(y + 1) * W * 3] for y in range(H))
    def ck(t, d):
        c = t + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n'
        + ck(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
        + ck(b'IDAT', zlib.compress(rows, 1)) + ck(b'IEND', b''))

colors = set(); overflow = False; n = 0; src = sys.stdin.buffer
while True:
    buf = b''
    while len(buf) < FS:
        chunk = src.read(FS - len(buf))
        if not chunk: break
        buf += chunk
    if len(buf) < FS: break
    if n in want:
        png('uniq/f%06d.png' % pos[n], buf)
        if not overflow:
            colors |= set(zip(buf[0::3], buf[1::3], buf[2::3]))
            if len(colors) > 256: overflow = True
    n += 1

# Up to 256 distinct colours means the GIF can carry the source palette verbatim.
# ffmpeg's palettegen averages the colours inside each box and can emit a shade
# the source never contained, so build the palette by hand whenever we can.
if not overflow:
    pal = bytearray()
    for c in sorted(colors): pal += bytes(bytearray(c)) + b'\xff'
    pal += b'\x00\x00\x00\x00' * (256 - len(colors))   # spare slots stay transparent so
    open('pal.rgba', 'wb').write(bytes(pal))           # ffmpeg keeps its inter-frame
f = open('palvars.sh', 'w', newline='\n')                            # transparency optimisation
f.write('N_COLORS=%d\nEXACT=%d\n' % (-1 if overflow else len(colors), 0 if overflow else 1))
f.close()
PYEOF

cat > gifdelays.py <<'PYEOF'
# Read the delays straight out of the GIF's Graphic Control Extension blocks.
# ffmpeg's own GIF demuxer re-times a variable-delay GIF onto a guessed constant
# frame rate, so asking ffmpeg would not tell us what we actually wrote; this is
# the same byte sequence a browser reads.
import sys
d = open(sys.argv[1], 'rb').read()
i = 13
if d[10] & 0x80: i += 3 * (2 << (d[10] & 7))         # skip global colour table
out = []
while i < len(d):
    b = d[i]
    if b == 0x21:                                     # extension block
        label = d[i + 1]; i += 2
        if label == 0xF9: out.append(d[i + 2] | (d[i + 3] << 8))
        while d[i]: i += d[i] + 1
        i += 1
    elif b == 0x2C:                                   # image descriptor
        flags = d[i + 9]; i += 10
        if flags & 0x80: i += 3 * (2 << (flags & 7))  # skip local colour table
        i += 1                                        # LZW minimum code size
        while d[i]: i += d[i] + 1
        i += 1
    else: break                                       # 0x3B trailer
sys.stdout.buffer.write(''.join('%d\n' % v for v in out).encode())  # bytes: no CRLF
PYEOF

# ------------------------------------------- pass 1: hash every decoded frame
"$FFMPEG" -hide_banner -v error -i "$IN_W" -an -vf "$FILTER" -f framemd5 - > frames.md5
python runs.py frames.md5 "$MIN_DELAY"
. ./vars.sh

# --------------------------- pass 2: write out only the first frame of each run
"$FFMPEG" -hide_banner -v error -i "$IN_W" -an -vf "$FILTER" -f rawvideo -pix_fmt rgb24 - \
  | python extract.py "$W" "$H"
. ./palvars.sh

if [ "$EXACT" = 1 ]; then
  "$FFMPEG" -hide_banner -v error -y -f rawvideo -pix_fmt rgba -s 16x16 -i pal.rgba palette.png
  USE_DITHER=none
else
  "$FFMPEG" -hide_banner -v error -y -i uniq/f%06d.png \
    -vf "palettegen=max_colors=256:stats_mode=full" palette.png
  USE_DITHER=$DITHER
fi

"$FFMPEG" -hide_banner -v error -y -f concat -safe 0 -i list.txt -i palette.png \
  -lavfi "[0:v][1:v]paletteuse=dither=$USE_DITHER:diff_mode=rectangle" \
  -fps_mode passthrough -frames:v "$N_UNIQUE" -loop "$LOOP" -final_delay "$LAST_DELAY" \
  out.gif

# ------------------------------------------------------- verify what we wrote
python gifdelays.py out.gif > got_delays.txt
# -fps_mode passthrough matters here: without it ffmpeg resamples the GIF to a
# guessed constant frame rate and silently duplicates frames on the way out.
"$FFMPEG" -hide_banner -v error -i out.gif -fps_mode passthrough -pix_fmt rgb24 \
  -f framemd5 - | grep -v '^#' | awk -F', *' '{print $6}' > got.hash
"$FFMPEG" -hide_banner -v error -i uniq/f%06d.png -fps_mode passthrough -pix_fmt rgb24 \
  -f framemd5 - | grep -v '^#' | awk -F', *' '{print $6}' > src.hash

TIMING_OK=1; cmp -s delays.txt got_delays.txt || TIMING_OK=0
PIXEL_OK=1;  cmp -s src.hash got.hash        || PIXEL_OK=0
DIFFER=$(paste src.hash got.hash | awk '$1 != $2' | wc -l)

cp out.gif "$OUT_W"
IN_SIZE=$(stat -c%s "$IN_W"); OUT_SIZE=$(stat -c%s out.gif)

printf '%s\n' "$OUT"
printf '  frames    %s -> %s unique\n' "$N_TOTAL" "$N_UNIQUE"
printf '  size      %sx%s, %s -> %s bytes\n' "$W" "$H" "$IN_SIZE" "$OUT_SIZE"
awk -v a="$TOTAL_CS" -v b="$SRC_CS" -v c="$CLAMPED" -v m="$MIN_DELAY" 'BEGIN{
  printf "  duration  %.2fs (source %.2fs", a/100, b/100
  if (c > 0) printf ", +%.2fs from %d frame(s) raised to the %dcs floor", (a-b)/100, c, m
  printf ")\n" }'
if [ "$EXACT" = 1 ]; then
  printf '  palette   %s colours, taken verbatim from the source\n' "$N_COLORS"
else
  printf '  palette   source needs >256 colours; quantised to 256 with dither=%s\n' "$DITHER"
fi
if [ "$TIMING_OK" = 1 ]; then
  printf '  timing    verified: all %s delays match\n' "$N_UNIQUE"
else
  printf '  timing    WARNING: delays differ from plan (ffmpeg too old for the concat "option" directive?)\n'
fi
if [ "$PIXEL_OK" = 1 ]; then
  printf '  pixels    verified: bit-identical to the source frames\n'
else
  printf '  pixels    %s of %s frames altered by quantisation/dithering\n' "$DIFFER" "$N_UNIQUE"
fi
[ "$KEEP" = 1 ] && printf '  temp      %s\n' "$TMP"
[ "$TIMING_OK" = 1 ] || exit 2
exit 0
