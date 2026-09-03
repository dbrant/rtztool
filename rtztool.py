#!/usr/bin/env python3
"""rtztool -- unpack and repack the data files of Return to Zork (MADE engine).

    rtztool prj info    RTZCD.PRJ
    rtztool prj list    RTZCD.PRJ [--type FLEX]
    rtztool prj extract RTZCD.PRJ mod/
    rtztool prj build   mod/ RTZCD.PRJ --source RTZCD.PRJ.orig
    rtztool prj get     RTZCD.PRJ FLEX MAPP1 out.png

    rtztool dat info    RTZCD.DAT
    rtztool dat extract RTZCD.DAT game/
    rtztool dat build   game/ RTZCD.DAT --source RTZCD.DAT.orig
    rtztool dat disasm  RTZCD.DAT 457

    rtztool red list    RTZCD.RED
    rtztool red extract RTZCD.RED out/

    rtztool pmv info    FINTRO00.PMV
    rtztool pmv extract FINTRO00.PMV movie/
    rtztool pmv build   movie/ FINTRO00.PMV

Run any subcommand with -h for its options.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from made import assets, datassets, resources, script          # noqa: E402
from made.dat import GameDatabase                              # noqa: E402
from made.prj import ProjectFile                               # noqa: E402


def fingerprint(path):
    """ScummVM identifies a game file by the md5 of its first 5000 bytes."""
    import hashlib
    with open(path, "rb") as f:
        return hashlib.md5(f.read(5000)).hexdigest()


def report_fingerprint(source, out):
    if fingerprint(source) == fingerprint(out):
        print("  detection fingerprint unchanged -- ScummVM still recognises the game")
    else:
        print("  WARNING: the detection fingerprint changed; ScummVM will report the")
        print("  game as an unknown variant until you add the new md5 to its tables")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


# --------------------------------------------------------------------------
# prj
# --------------------------------------------------------------------------

def cmd_prj_info(args):
    prj = ProjectFile.load(args.archive)
    total = os.path.getsize(args.archive)
    print("%s  %s" % (args.archive, human(total)))
    print("  header      unk1=%d unk2=%d ddit=%d/%d"
          % (prj.header_unk1, prj.header_unk2, prj.ddit_a, prj.ddit_b))
    print("  %-6s %8s %8s %10s  %s" % ("type", "count", "capacity", "bytes", "names"))
    for tag in prj.type_order:
        name = tag.decode("ascii")
        if tag in (b"FREE", b"ARCH"):
            extra = "%d free blocks" % len(prj.free_blocks) if tag == b"FREE" else ""
            print("  %-6s %8s %8d %10s  %s"
                  % (name, "-", prj.capacities.get(tag, 0), "-", extra))
            continue
        table = prj.resources.get(tag, {})
        size = sum(len(r.payload) for r in table.values())
        print("  %-6s %8d %8d %10s  %s"
              % (name, len(table), prj.capacities.get(tag, 0), human(size),
                 "yes" if prj.has_symb.get(tag) else "no"))


def cmd_prj_list(args):
    prj = ProjectFile.load(args.archive)
    want = set(t.upper() for t in args.type.split(",")) if args.type else None
    for tag in prj.types():
        name = tag.decode("ascii")
        if want and name not in want:
            continue
        for index in sorted(prj.resources[tag]):
            res = prj.resources[tag][index]
            detail = ""
            if tag == b"FLEX":
                try:
                    pic = resources.decode_picture(res.payload)
                    detail = "%dx%d%s" % (pic.width, pic.height,
                                          " pal" if pic.palette else "")
                except Exception:
                    detail = "?"
            elif tag == b"ANIM":
                try:
                    anim = resources.decode_anim(res.payload, accumulate=False)
                    detail = "%dx%d %d frames%s" % (anim.width, anim.height,
                                                    len(anim.frames),
                                                    " delta" if anim.is_delta else "")
                except Exception:
                    detail = "?"
            elif tag == b"MENU":
                try:
                    detail = "%d strings" % len(resources.decode_menu(res.payload).strings)
                except Exception:
                    detail = "?"
            print("%-5s %5d  %-17s %9d  %s"
                  % (name, index, res.name, len(res.payload), detail))


def cmd_prj_extract(args):
    types = [t.upper() for t in args.type.split(",")] if args.type else None
    seen = [0]

    def progress(type_name, index, name):
        seen[0] += 1
        if seen[0] % 200 == 0:
            sys.stderr.write("\r  %d resources..." % seen[0])
            sys.stderr.flush()

    manifest = assets.extract(args.archive, args.outdir, sound_rate=args.rate,
                              palette_name=args.palette, only_types=types,
                              progress=progress)
    sys.stderr.write("\r" + " " * 40 + "\r")
    total = sum(len(t["resources"]) for t in manifest["types"].values())
    print("extracted %d resources to %s" % (total, args.outdir))
    for name, info in sorted(manifest["types"].items()):
        print("  %-5s %5d" % (name, len(info["resources"])))
    print("reference palette: %s" % (manifest["reference_palette"] or "greyscale"))


def cmd_prj_build(args):
    def progress(type_name, index, name, dirty):
        print("  re-encoding %s #%d %s" % (type_name, index, name))

    changed, size, result = assets.build(
        args.indir, args.source, args.out, allow_raw=not args.lossy,
        progress=progress, force=args.force, compact=args.compact)
    print("wrote %s (%s), %d resource(s) re-encoded" % (args.out, human(size), changed))
    if result is not None and changed:
        print("  %d written in place, %d appended" % (result.in_place, result.appended))
    report_fingerprint(args.source, args.out)


def cmd_prj_get(args):
    prj = ProjectFile.load(args.archive)
    tag = args.type.upper().encode("ascii")
    if args.name.isdigit():
        res = prj.get(tag, int(args.name))
    else:
        res = prj.by_name(tag, args.name)
    if res is None:
        sys.exit("no such resource: %s %s" % (args.type, args.name))
    if args.raw:
        with open(args.out, "wb") as f:
            f.write(res.payload)
    elif tag == b"FLEX":
        from made import pngio
        pic = resources.decode_picture(res.payload)
        pal = pic.palette or assets.pick_reference_palette(prj)[0]
        pngio.write_palette_png(args.out, pic.width, pic.height, pic.indices, pal)
    elif tag == b"SNDS":
        snd = resources.decode_sound(res.payload)
        assets.write_wav(args.out, snd.samples, args.rate)
    elif tag == b"MENU":
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(assets.menu_to_text(resources.decode_menu(res.payload)))
    else:
        with open(args.out, "wb") as f:
            f.write(res.payload)
    print("wrote %s" % args.out)


# --------------------------------------------------------------------------
# dat
# --------------------------------------------------------------------------

def cmd_dat_info(args):
    db = GameDatabase.load(args.database)
    code = db.code_objects()
    text = db.text_objects(2)
    print("%s  %s" % (args.database, human(os.path.getsize(args.database))))
    print("  program name        %s" % db.program_name.rstrip(b"\x00").decode("latin-1"))
    print("  objects             %d" % len(db.objects))
    print("  main code object    %d" % db.main_code_index)
    print("  game state          %d bytes at 0x%X" % (len(db.game_state), db.game_state_offs))
    print("  constant block      %d bytes at 0x%X" % (db.objects_size, db.objects_offs))
    print("  scripts             %d" % len(code))
    print("  text byte arrays    %d" % len(text))
    kinds = {"object": 0, "words": 0, "bytes": 0}
    for o in db.objects:
        kinds["object" if o.is_object else "words" if o.is_word_array else "bytes"] += 1
    print("  object classes      %d real, %d word arrays, %d byte arrays"
          % (kinds["object"], kinds["words"], kinds["bytes"]))


def cmd_dat_extract(args):
    stats = datassets.extract(args.database, args.outdir)
    print("extracted %d objects to %s" % (stats["objects"], args.outdir))
    print("  %d scripts, %d text strings" % (stats["scripts"], stats["strings"]))


def cmd_dat_build(args):
    result = datassets.build(args.indir, args.source, args.out)
    print("wrote %s (%s)" % (args.out, human(result["size"])))
    print("  %d object(s) changed, %d relocated" % (len(result["changed"]), len(result["moved"])))
    report_fingerprint(args.source, args.out)


def cmd_dat_disasm(args):
    db = GameDatabase.load(args.database)
    obj = db.get(args.index)
    if obj is None:
        sys.exit("no object %d" % args.index)
    if not obj.is_byte_array:
        sys.exit("object %d is not a byte array (class 0x%04X)" % (args.index, obj.cls))
    sys.stdout.write(script.disassemble(obj.data, name="object %d" % args.index, objects=db))


def cmd_dat_strings(args):
    db = GameDatabase.load(args.database)
    for index in db.text_objects(args.min_len):
        print("%5d  %s" % (index, db.get(index).text()))


# --------------------------------------------------------------------------
# red
# --------------------------------------------------------------------------

def cmd_red_list(args):
    from made.red import list_files
    for entry in list_files(args.archive):
        print("%-14s %9d -> %9d" % (entry.name, entry.comp_size, entry.orig_size))


def cmd_red_extract(args):
    from made.red import list_files, extract_file
    os.makedirs(args.outdir, exist_ok=True)
    for entry in list_files(args.archive):
        if args.name and entry.name.upper() != args.name.upper():
            continue
        data = extract_file(args.archive, entry.name)
        path = os.path.join(args.outdir, entry.name)
        with open(path, "wb") as f:
            f.write(data)
        print("%-14s %d bytes" % (entry.name, len(data)))


# --------------------------------------------------------------------------
# pmv
# --------------------------------------------------------------------------

def cmd_pmv_info(args):
    from made import pmv
    movie = pmv.load(args.movie, decode_video=False)
    print("%s  %s" % (args.movie, human(os.path.getsize(args.movie))))
    print("  frames        %d" % len(movie.frames))
    print("  frame delay   %d ms (%.1f fps)"
          % (movie.frame_delay, 1000.0 / movie.frame_delay if movie.frame_delay else 0))
    print("  video         %dx%d" % (movie.width, movie.height))
    print("  audio         %d Hz, %d samples (%.1f s)"
          % (movie.sound_freq, len(movie.audio),
             len(movie.audio) / float(movie.sound_freq or 1)))
    print("  palette resets %d" % sum(1 for f in movie.frames if f.palette_updates))


def cmd_pmv_extract(args):
    from made import pmv
    meta = pmv.extract(args.movie, args.outdir, write_frames=not args.audio_only)
    print("extracted %d frames (%dx%d) to %s"
          % (meta["frame_count"], meta["width"], meta["height"], args.outdir))


def cmd_pmv_build(args):
    from made import pmv
    size = pmv.build(args.indir, args.out)
    print("wrote %s (%s)" % (args.out, human(size)))


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="rtztool", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="group")

    prj = sub.add_parser("prj", help="resource archive (RTZCD.PRJ)").add_subparsers(dest="cmd")

    p = prj.add_parser("info", help="summarise an archive")
    p.add_argument("archive")
    p.set_defaults(func=cmd_prj_info)

    p = prj.add_parser("list", help="list resources")
    p.add_argument("archive")
    p.add_argument("--type", help="comma separated type filter, e.g. FLEX,MENU")
    p.set_defaults(func=cmd_prj_list)

    p = prj.add_parser("extract", help="unpack every resource into editable files")
    p.add_argument("archive")
    p.add_argument("outdir")
    p.add_argument("--type", help="comma separated type filter")
    p.add_argument("--rate", type=int, default=assets.DEFAULT_SOUND_RATE,
                   help="sample rate written into the .wav files (default 11025)")
    p.add_argument("--palette", help="FLEX resource whose palette colours "
                                     "sprites and animations (default: first 320px picture)")
    p.set_defaults(func=cmd_prj_extract)

    p = prj.add_parser("build", help="repack an extracted tree")
    p.add_argument("indir")
    p.add_argument("out")
    p.add_argument("--source", required=True, help="the original archive to patch")
    p.add_argument("--force", action="store_true", help="re-encode everything")
    p.add_argument("--lossy", action="store_true",
                   help="never emit raw 16-pixel blocks; reduce busy blocks to "
                        "four colours instead (smaller, matches the original encoder)")
    p.add_argument("--compact", action="store_true",
                   help="rewrite the whole archive instead of patching it")
    p.set_defaults(func=cmd_prj_build)

    p = prj.add_parser("get", help="export a single resource")
    p.add_argument("archive")
    p.add_argument("type")
    p.add_argument("name", help="resource name or numeric index")
    p.add_argument("out")
    p.add_argument("--raw", action="store_true", help="write the undecoded payload")
    p.add_argument("--rate", type=int, default=assets.DEFAULT_SOUND_RATE)
    p.set_defaults(func=cmd_prj_get)

    dat = sub.add_parser("dat", help="game database (RTZCD.DAT)").add_subparsers(dest="cmd")

    p = dat.add_parser("info", help="summarise the database")
    p.add_argument("database")
    p.set_defaults(func=cmd_dat_info)

    p = dat.add_parser("extract", help="decompile objects, text and scripts")
    p.add_argument("database")
    p.add_argument("outdir")
    p.set_defaults(func=cmd_dat_extract)

    p = dat.add_parser("build", help="recompile a decompiled database")
    p.add_argument("indir")
    p.add_argument("out")
    p.add_argument("--source", required=True, help="the original database")
    p.set_defaults(func=cmd_dat_build)

    p = dat.add_parser("disasm", help="disassemble one script object")
    p.add_argument("database")
    p.add_argument("index", type=int)
    p.set_defaults(func=cmd_dat_disasm)

    p = dat.add_parser("strings", help="print every text object")
    p.add_argument("database")
    p.add_argument("--min-len", type=int, default=2)
    p.set_defaults(func=cmd_dat_strings)

    red = sub.add_parser("red", help="LZH archive (RTZCD.RED)").add_subparsers(dest="cmd")

    p = red.add_parser("list", help="list the archive contents")
    p.add_argument("archive")
    p.set_defaults(func=cmd_red_list)

    p = red.add_parser("extract", help="decompress files out of the archive")
    p.add_argument("archive")
    p.add_argument("outdir")
    p.add_argument("--name", help="extract only this file")
    p.set_defaults(func=cmd_red_extract)

    pmv = sub.add_parser("pmv", help="cutscene movies (*.PMV)").add_subparsers(dest="cmd")

    p = pmv.add_parser("info", help="summarise a movie")
    p.add_argument("movie")
    p.set_defaults(func=cmd_pmv_info)

    p = pmv.add_parser("extract", help="unpack frames and audio")
    p.add_argument("movie")
    p.add_argument("outdir")
    p.add_argument("--audio-only", action="store_true",
                   help="skip video decoding (much faster)")
    p.set_defaults(func=cmd_pmv_extract)

    p = pmv.add_parser("build", help="reassemble a movie")
    p.add_argument("indir")
    p.add_argument("out")
    p.set_defaults(func=cmd_pmv_build)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
