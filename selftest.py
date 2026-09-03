#!/usr/bin/env python3
"""Self-test for rtztool against a real Return to Zork installation.

    python3 selftest.py [game directory]

Checks that every resource decodes, that the documented checksums hold, and
that unpack/repack is faithful.  Exits non-zero if anything fails.
"""

import os
import random
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from made import assets, datassets, imagecodec, pmv, red, resources, script
from made.dat import GameDatabase
from made.prj import ProjectFile, patch_archive

FAILURES = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    status = "ok  " if ok else "FAIL"
    print("  [%s] %s%s" % (status, name, ("  -- " + detail) if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def find_files(root):
    prj = dat = redf = None
    movies = []
    for base, _dirs, names in os.walk(root):
        for n in names:
            up = n.upper()
            path = os.path.join(base, n)
            if up.endswith(".PRJ") and prj is None:
                prj = path
            elif up.endswith(".DAT") and up.startswith("RTZ") and dat is None:
                dat = path
            elif up in ("RTZCD.RED", "RTZ.RED") and redf is None:
                redf = path
            elif up.endswith(".PMV"):
                movies.append(path)
    movies.sort(key=os.path.getsize)
    return prj, dat, redf, movies


def test_prj(path):
    print("\n%s" % path)
    prj = ProjectFile.load(path)
    raw = open(path, "rb").read()

    check("archive parses", len(prj.types()) == 6)

    bad = 0
    for res in prj.iter_resources():
        stored = struct.unpack_from("<I", raw, res.src_offset + 8)[0]
        body = raw[res.src_offset + 12:res.src_offset + res.src_size]
        if stored != (sum(body) & 0xFFFFFFFF):
            bad += 1
    check("every DATA header checksum verifies", bad == 0, "%d mismatches" % bad)

    counts = {}
    errors = []
    for tag, fn in ((b"FLEX", resources.decode_picture),
                    (b"ANIM", resources.decode_anim),
                    (b"SNDS", resources.decode_sound),
                    (b"MENU", resources.decode_menu),
                    (b"FONT", resources.decode_font)):
        n = 0
        for res in prj.resources[tag].values():
            try:
                fn(res.payload)
                n += 1
            except Exception as exc:                            # noqa: BLE001
                errors.append("%s/%s: %s" % (tag.decode(), res.name, exc))
        counts[tag] = n
    check("every resource decodes",
          not errors,
          "; ".join(errors[:3]))
    print("       %s" % ", ".join("%s %d" % (t.decode(), n) for t, n in counts.items()))

    bad = [r.name for r in prj.resources[b"MENU"].values()
           if resources.encode_menu(resources.decode_menu(r.payload)) != r.payload]
    check("MENU re-encodes byte for byte", not bad, str(bad[:3]))

    bad = [r.name for r in prj.resources[b"FONT"].values()
           if resources.encode_font(resources.decode_font(r.payload)) != r.payload]
    check("FONT re-encodes byte for byte", not bad, str(bad[:3]))

    random.seed(20240101)
    pictures = list(prj.resources[b"FLEX"].values())
    sample = pictures if os.environ.get("RTZ_FULL") else random.sample(pictures, 40)
    bad = []
    grew = 0
    for res in sample:
        pic = resources.decode_picture(res.payload)
        enc = resources.encode_picture(pic, allow_raw=True)
        again = resources.decode_picture(enc)
        if bytes(again.indices) != bytes(pic.indices) or again.palette != pic.palette:
            bad.append(res.name)
        grew += len(enc) - len(res.payload)
    check("pictures survive a lossless re-encode (%d tested)" % len(sample), not bad, str(bad[:3]))
    print("       size delta over the sample: %+d bytes" % grew)

    anims = list(prj.resources[b"ANIM"].values())
    sample = anims if os.environ.get("RTZ_FULL") else random.sample(anims, 25)
    bad = []
    for res in sample:
        anim = resources.decode_anim(res.payload, accumulate=False)
        again = resources.decode_anim(resources.encode_anim(anim, allow_raw=True),
                                      accumulate=False)
        if len(again.frames) != len(anim.frames) or any(
                bytes(a.indices) != bytes(b.indices)
                for a, b in zip(anim.frames, again.frames)):
            bad.append(res.name)
    check("animations survive a lossless re-encode (%d tested)" % len(sample), not bad, str(bad[:3]))

    sounds = list(prj.resources[b"SNDS"].values())
    sample = sounds if os.environ.get("RTZ_FULL") else random.sample(sounds, 25)
    bad = [r.name for r in sample
           if resources.decode_sound(
               resources.encode_sound(resources.decode_sound(r.payload))).samples
           != resources.decode_sound(r.payload).samples]
    check("sounds survive a lossless re-encode (%d tested)" % len(sample), not bad, str(bad[:3]))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.prj")
        patch_archive(path, out, {})
        check("an empty patch reproduces the archive exactly",
              open(out, "rb").read() == raw)
    return prj


def test_prj_roundtrip(path):
    print("\nunpack / repack")
    with tempfile.TemporaryDirectory() as tmp:
        tree = os.path.join(tmp, "mod")
        assets.extract(path, tree, only_types=["MENU", "FONT", "ANIM"])
        out = os.path.join(tmp, "rebuilt.prj")
        changed, _size, _res = assets.build(tree, path, out)
        check("an untouched rebuild changes nothing", changed == 0)
        check("an untouched rebuild is byte-identical",
              open(out, "rb").read() == open(path, "rb").read())

        # now actually edit something and confirm it lands
        menu_file = None
        for name in os.listdir(os.path.join(tree, "MENU")):
            if name == "MEN_ACTIONS.txt":
                menu_file = os.path.join(tree, "MENU", name)
        lines = open(menu_file, encoding="utf-8").read().split("\n")
        lines[0] = "SELF TEST"
        open(menu_file, "w", encoding="utf-8").write("\n".join(lines))
        changed, _size, result = assets.build(tree, path, out)
        check("editing one file re-encodes exactly one resource", changed == 1)
        rebuilt = ProjectFile.load(out)
        menu = resources.decode_menu(rebuilt.by_name(b"MENU", "MEN_ACTIONS").payload)
        check("the edit is readable from the rebuilt archive",
              menu.strings[0] == "SELF TEST", menu.strings[0])
        import hashlib
        a = hashlib.md5(open(path, "rb").read(5000)).hexdigest()
        b = hashlib.md5(open(out, "rb").read(5000)).hexdigest()
        check("the ScummVM detection fingerprint is preserved", a == b)


def test_dat(path):
    print("\n%s" % path)
    db = GameDatabase.load(path)
    check("database parses", len(db.objects) > 0)
    print("       %d objects, main script %d" % (len(db.objects), db.main_code_index))

    code = db.code_objects()
    bad = []
    for index in code:
        obj = db.get(index)
        text = script.disassemble(obj.data, objects=db)
        if script.assemble(text) != obj.data:
            bad.append(index)
    check("every script disassembles and re-assembles exactly (%d scripts)" % len(code),
          not bad, str(bad[:5]))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.dat")
        db.save(out)
        check("an untouched save is byte-identical",
              open(out, "rb").read() == open(path, "rb").read())

        tree = os.path.join(tmp, "game")
        datassets.extract(path, tree)
        out2 = os.path.join(tmp, "rebuilt.dat")
        result = datassets.build(tree, path, out2)
        check("a full decompile/recompile is byte-identical",
              open(out2, "rb").read() == open(path, "rb").read(),
              "%d objects changed" % len(result["changed"]))


def test_red(path, cd_dir):
    print("\n%s" % path)
    entries = red.list_files(path)
    check("archive lists its files", len(entries) > 0)
    print("       %s" % ", ".join(e.name for e in entries))
    # The installer rewrites these two on disk (a Program Manager group
    # checksum and PIF settings), so the loose copies legitimately differ.
    mutable = {"RTZ.GRP", "RTZ.PIF"}
    compared = 0
    bad = []
    differs = []
    for entry in entries:
        data = red.extract_file(path, entry.name)
        if len(data) != entry.orig_size:
            bad.append(entry.name + " (size)")
            continue
        twin = os.path.join(cd_dir, entry.name)
        if not os.path.exists(twin) and entry.name == "READMECD.ENG":
            twin = os.path.join(cd_dir, "READ.ME")
        if os.path.exists(twin):
            if open(twin, "rb").read() != data:
                (differs if entry.name in mutable else bad).append(entry.name)
            else:
                compared += 1
    check("LZH decompression matches the loose copies (%d compared)" % compared,
          not bad and compared > 0, str(bad[:3]) or "nothing to compare against")
    if differs:
        print("       (%s differ on disk; the installer rewrites them)"
              % ", ".join(differs))


def test_pmv(paths):
    if not paths:
        return
    path = paths[0]
    print("\n%s" % path)
    movie = pmv.load(path)
    check("movie parses", len(movie.frames) == movie.frame_count)
    print("       %d frames, %dx%d, %d Hz" % (len(movie.frames), movie.width,
                                              movie.height, movie.sound_freq))
    with tempfile.TemporaryDirectory() as tmp:
        tree = os.path.join(tmp, "movie")
        pmv.extract(path, tree)
        out = os.path.join(tmp, "rebuilt.pmv")
        pmv.build(tree, out)
        again = pmv.load(out)
        check("rebuilt movie has the same frames",
              len(again.frames) == len(movie.frames))
        check("every frame is pixel-identical after a rebuild",
              all(a.image == b.image for a, b in zip(movie.frames, again.frames)))
        check("the soundtrack is sample-identical after a rebuild",
              again.audio == movie.audio)
        print("       %d bytes -> %d bytes" % (os.path.getsize(path),
                                               os.path.getsize(out)))


def test_codec_units():
    print("\ncodec unit checks")
    random.seed(7)
    bad = []
    for width, height in ((1, 1), (8, 6), (25, 21), (33, 21), (39, 6), (90, 79),
                          (163, 85), (320, 200), (317, 3)):
        src = bytes(random.randrange(256) for _ in range(width * height))
        cmd, pixel, mask = imagecodec.compress_image(src, width, height, allow_raw=True)
        blob = bytearray(64)
        cmd_offs = 64
        pixel_offs = cmd_offs + len(cmd)
        mask_offs = pixel_offs + len(pixel)
        blob += cmd + pixel + mask
        out = imagecodec.decompress_image(bytes(blob), width, height, cmd_offs,
                                          pixel_offs, mask_offs,
                                          imagecodec.line_size_for(width))
        if bytes(out) != src:
            bad.append("%dx%d" % (width, height))
    check("random images survive the still-picture codec at odd sizes",
          not bad, str(bad))

    bad = []
    for width, height in ((17, 9), (320, 200), (240, 148)):
        a = bytes(random.randrange(4) for _ in range(width * height))
        b = bytearray(a)
        for _ in range(50):
            b[random.randrange(len(b))] = random.randrange(4)
        cmd, pixel, mask = imagecodec.compress_movie_image(bytes(b), width, height, a)
        blob = bytearray(26)
        cmd_offs = 26
        pixel_offs = cmd_offs + len(cmd)
        mask_offs = pixel_offs + len(pixel)
        blob += cmd + pixel + mask
        out = imagecodec.decompress_movie_image(bytes(blob), width, height, cmd_offs,
                                                pixel_offs, mask_offs,
                                                imagecodec.line_size_for(width), prev=a)
        if bytes(out) != bytes(b):
            bad.append("%dx%d" % (width, height))
    check("random frames survive the movie codec", not bad, str(bad))


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..")
    prj, dat, redf, movies = find_files(root)
    if not prj:
        sys.exit("no .PRJ found under %s" % root)

    test_codec_units()
    test_prj(prj)
    test_prj_roundtrip(prj)
    if dat:
        test_dat(dat)
    if redf:
        test_red(redf, os.path.dirname(prj))
    test_pmv(movies)

    print("\n%d checks, %d failures" % (CHECKS[0], len(FAILURES)))
    for name in FAILURES:
        print("  FAILED: %s" % name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
